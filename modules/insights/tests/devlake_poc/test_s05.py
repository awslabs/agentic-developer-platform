import copy
import hashlib
import json
import unittest
from pathlib import Path

from modules.insights.poc.devlake.evidence_chain import capture_record


FIXTURES = Path(__file__).resolve().parents[2] / "schemas/fixtures/v2"


class CaptureEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((FIXTURES / "story_revision.json").read_text())
        self.source_bytes = b'{"issue":42,"revision":1}'
        self.snapshot = {"artifact_id": "private-source", "version_id": "version-1",
                         "sha256": hashlib.sha256(self.source_bytes).hexdigest()}
        self.record["source_snapshot"] = self.snapshot

    def test_capture_exact_reference_and_authority_restricted_dependencies(self):
        resolved = []

        def authority(reference):
            resolved.append(copy.deepcopy(reference))
            return {"realm_id": "controlled-realm", "visibility": "private" if "artifact_id" in reference else "shared",
                    "policy_ref": "authority-policy", "policy_revision": 2}

        result = capture_record(self.record, self.snapshot, self.source_bytes, authority)
        self.assertEqual(result["reference"]["record_revision"], "revision-1")
        self.assertEqual(result["record"]["source_snapshot"], self.snapshot)
        self.assertEqual(result["audience"], "private")
        self.assertEqual(len(resolved), 2)
        self.assertIn(self.snapshot, resolved)
        self.record["title"] = "subsequent edit"
        self.assertEqual(result["record"]["title"], "Invite a member")

    def test_refuse_missing_bytes_or_policy_before_capture(self):
        authority = lambda reference: {"realm_id": "controlled-realm", "visibility": "shared",
                                       "policy_ref": "authority-policy", "policy_revision": 1}
        with self.assertRaisesRegex(ValueError, "exact source snapshot"):
            capture_record(self.record, self.snapshot, b"different", authority)
        with self.assertRaisesRegex(ValueError, "authority did not resolve"):
            capture_record(self.record, self.snapshot, self.source_bytes, lambda reference: None)
        self.record["source_snapshot"] = {**self.snapshot, "version_id": "version-2"}
        with self.assertRaisesRegex(ValueError, "source snapshot differs"):
            capture_record(self.record, self.snapshot, self.source_bytes, authority)


if __name__ == "__main__":
    unittest.main()


class TestResultCaptureTest(unittest.TestCase):
    def test_source_attempt_and_artifact_dependencies_are_qualified(self):
        record = json.loads((FIXTURES / "test_result.json").read_text())
        source_bytes = b'{"run":"RUN-001","attempt":1,"result":"passed"}'
        source_snapshot = {"artifact_id": "ci-test-report", "version_id": "attempt-1",
                           "sha256": hashlib.sha256(source_bytes).hexdigest()}
        seen = []

        def authority(reference):
            seen.append(reference)
            return {"realm_id": "controlled-realm", "visibility": "private" if "artifact_id" in reference else "shared",
                    "policy_ref": "authority-policy", "policy_revision": 2}

        captured = capture_record(record, source_snapshot, source_bytes, authority)
        self.assertEqual(captured["record"]["attempt"], 1)
        self.assertEqual(captured["reference"]["record_type"], "test_result")
        self.assertEqual(captured["audience"], "private")
        self.assertIn(record["run_test_ref"], seen)
        self.assertIn(record["artifact_refs"][0], seen)
        self.assertIn(source_snapshot, seen)

    def test_unknown_dependency_scope_is_not_admitted(self):
        record = json.loads((FIXTURES / "test_result.json").read_text())
        source_bytes = b"test-report"
        snapshot = {"artifact_id": "report", "version_id": "v1",
                    "sha256": hashlib.sha256(source_bytes).hexdigest()}

        def authority(reference):
            return {"realm_id": "other-realm" if "artifact_id" in reference else "controlled-realm",
                    "visibility": "shared", "policy_ref": "authority-policy", "policy_revision": 2}

        with self.assertRaisesRegex(ValueError, "same realm"):
            capture_record(record, snapshot, source_bytes, authority)


class RevisionHistoryTest(unittest.TestCase):
    def setUp(self):
        self.first = json.loads((FIXTURES / "acceptance_criterion.json").read_text())
        self.second = copy.deepcopy(self.first)
        self.second.update(record_revision="revision-2", source_event_id="issue-42/AC-001:revision-2",
                           event_time="2026-10-11T10:00:00Z", text="An invitation needs a verified domain.")
        self.second["story_ref"]["record_revision"] = "revision-2"
        self.assessment = json.loads((FIXTURES / "acceptance_assessment.json").read_text())

    def capture(self, record):
        source_bytes = json.dumps(record, sort_keys=True).encode()
        snapshot = {"artifact_id": record["source_event_id"], "version_id": record["record_revision"],
                    "sha256": hashlib.sha256(source_bytes).hexdigest()}
        return capture_record(record, snapshot, source_bytes, lambda reference: {
            "realm_id": "controlled-realm", "visibility": "private",
            "policy_ref": "authority-policy", "policy_revision": 2})

    def test_edited_criterion_keeps_old_assessment_without_inheriting_acceptance(self):
        from modules.insights.poc.devlake.evidence_chain import criterion_history

        captured = [self.capture(record) for record in (self.first, self.second, self.assessment)]
        history = criterion_history(captured, self.first["story_ref"], "AC-001")
        self.assertEqual([item["criterion"]["text"] for item in history],
                         [self.first["text"], self.second["text"]])
        self.assertEqual([item["source_snapshot"]["version_id"] for item in history],
                         ["revision-1", "revision-2"])
        self.assertEqual(history[0]["assessments"][0]["verdict"], "accepted")
        self.assertEqual(history[0]["assessments"][0]["assessor_id"], "evaluator-example")
        self.assertEqual(history[1]["assessments"], [])

    def test_conflicting_or_mutated_revisions_are_rejected(self):
        from modules.insights.poc.devlake.evidence_chain import criterion_history

        captured = self.capture(self.first)
        captured["record"]["text"] = "tampered"
        with self.assertRaisesRegex(ValueError, "capture changed"):
            criterion_history([captured], self.first["story_ref"], "AC-001")
        first = self.capture(self.first)
        conflicting = self.capture(self.first)
        conflicting["record"]["text"] = "conflict"
        conflicting["record_sha256"] = hashlib.sha256(json.dumps(conflicting["record"], sort_keys=True,
                                                            separators=(",", ":")).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "conflicting bytes"):
            criterion_history([first, conflicting], self.first["story_ref"], "AC-001")


class ExactOfflineLinkTest(unittest.TestCase):
    def setUp(self):
        self.records = [json.loads((FIXTURES / f"{name}.json").read_text()) for name in
                        ("acceptance_criterion", "test_definition", "run_test", "test_run", "test_result")]
        test_run = self.records[3]
        self.artifact = {"artifact_ref": {"artifact_id": "built-image", "version_id": "sha256-image",
                                          "sha256": "b" * 64},
                         "source_revision": test_run["source_revision"],
                         "selection_snapshot": test_run["selection_snapshot"]}
        self.receipt = {"artifact_ref": copy.deepcopy(self.artifact["artifact_ref"]),
                        "source_revision": test_run["source_revision"], "environment": "dev",
                        "source_snapshot": {"artifact_id": "dev-receipt", "version_id": "delivery-1",
                                            "sha256": "c" * 64}}

    def test_evidence_chain(self):
        from modules.insights.poc.devlake.evidence_chain import match_delivery

        link = match_delivery(*self.records, self.artifact, self.receipt)
        self.assertEqual(link["criterion_ref"], self.records[2]["criterion_refs"][0])
        self.assertEqual(link["test_ref"], self.records[2]["test_ref"])
        self.assertEqual(link["result_ref"], {field: self.records[4][field] for field in
                                               ("tenant_id", "project_id", "source_connection_id",
                                                "record_type", "record_id", "record_revision")})
        self.assertEqual(link["tested_artifact"], self.receipt["artifact_ref"])
        self.assertEqual(link["delivery"]["environment"], "dev")
        self.assertEqual(link["delivery"]["status"], "matched_input")
        self.assertEqual(link["selection_snapshot"], self.records[3]["selection_snapshot"])

    def test_missing_delivery_stays_unknown(self):
        from modules.insights.poc.devlake.evidence_chain import match_delivery

        self.assertEqual(match_delivery(*self.records, self.artifact, None)["delivery"], {"status": "unknown"})
        receipt = {**self.receipt, "environment": None}
        self.assertEqual(match_delivery(*self.records, self.artifact, receipt)["delivery"], {"status": "unknown"})


class NegativeChainTest(unittest.TestCase):
    def setUp(self):
        self.case = ExactOfflineLinkTest()
        self.case.setUp()

    def test_wrong_deployed_digest_and_source_are_rejected(self):
        from modules.insights.poc.devlake.evidence_chain import match_delivery

        mismatched = copy.deepcopy(self.case.receipt)
        mismatched["artifact_ref"]["sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "delivery digest"):
            match_delivery(*self.case.records, self.case.artifact, mismatched)
        mismatched = {**self.case.receipt, "source_revision": "older-source"}
        with self.assertRaisesRegex(ValueError, "tested code revision"):
            match_delivery(*self.case.records, self.case.artifact, mismatched)
        wrong_selection = {**self.case.artifact, "selection_snapshot": None}
        with self.assertRaisesRegex(ValueError, "CI selection"):
            match_delivery(*self.case.records, wrong_selection, self.case.receipt)

    def test_authority_private_dependency_refuses_requested_shared_capture(self):
        story = json.loads((FIXTURES / "story_revision.json").read_text())
        source_bytes = b"controlled private issue"
        snapshot = {"artifact_id": "private-snapshot", "version_id": "v1",
                    "sha256": hashlib.sha256(source_bytes).hexdigest()}
        story["source_snapshot"] = snapshot
        inspected = []

        def authority(reference):
            inspected.append(reference)
            return {"realm_id": "restricted-realm", "visibility": "private" if "artifact_id" in reference else "shared",
                    "policy_ref": "authority-policy", "policy_revision": 3}

        with self.assertRaisesRegex(ValueError, "private dependency"):
            capture_record(story, snapshot, source_bytes, authority, requested_audience="shared")
        self.assertIn(snapshot, inspected)
        candidate = capture_record(story, snapshot, source_bytes, authority, requested_audience="private")
        self.assertEqual(candidate["audience"], "private")


class AttemptCoverageTest(unittest.TestCase):
    def setUp(self):
        self.run = json.loads((FIXTURES / "test_run.json").read_text())
        self.selected = json.loads((FIXTURES / "run_test.json").read_text())
        self.failed = json.loads((FIXTURES / "test_result.json").read_text())
        self.failed["outcome"] = "failed"
        self.passed = copy.deepcopy(self.failed)
        self.passed.update(attempt=2, outcome="passed", record_id=self.failed["record_id"] + "/2",
                           source_event_id=self.failed["source_event_id"] + "/2")
        self.missing = copy.deepcopy(self.selected)
        self.missing.update(record_id=self.selected["record_id"] + "/missing",
                            instance_id="missing", source_event_id=self.selected["source_event_id"] + "/missing")

    def test_attempts_and_missing(self):
        from modules.insights.poc.devlake.evidence_chain import attempts_and_missing

        report = attempts_and_missing(self.run, [self.selected, self.missing], [self.passed, self.failed])
        by_instance = {item["instance_ref"]["record_id"]: item for item in report["instances"]}
        observed = by_instance[self.selected["record_id"]]
        self.assertEqual([(entry["attempt"], entry["outcome"]) for entry in observed["attempts"]],
                         [(1, "failed"), (2, "passed")])
        self.assertEqual(by_instance[self.missing["record_id"]]["status"], "missing")
        self.assertEqual(by_instance[self.missing["record_id"]]["attempts"], [])
        self.assertEqual(report["completeness_percent"], 50)
        self.assertEqual(report["acceptance"], "not_assessed")

    def test_rerun_does_not_merge_retry_history(self):
        from modules.insights.poc.devlake.evidence_chain import attempts_and_missing

        rerun = {**self.run, "source_attempt": 2, "record_id": "RUN-001-rerun",
                 "source_event_id": "RUN-001-rerun:revision-1"}
        new_instance = copy.deepcopy(self.selected)
        new_instance["run_ref"]["record_id"] = rerun["record_id"]
        rerun_result = copy.deepcopy(self.failed)
        rerun_result["run_test_ref"] = {field: new_instance[field] for field in
                                        ("tenant_id", "project_id", "source_connection_id",
                                         "record_type", "record_id", "record_revision")}
        first = attempts_and_missing(self.run, [self.selected], [self.failed, self.passed])
        second = attempts_and_missing(rerun, [new_instance], [rerun_result])
        self.assertEqual(first["source_attempt"], 1)
        self.assertEqual(second["source_attempt"], 2)
        self.assertEqual(len(first["instances"][0]["attempts"]), 2)
        self.assertEqual(len(second["instances"][0]["attempts"]), 1)
        self.assertNotEqual(first["run_ref"], second["run_ref"])

    def test_green_run_cannot_invent_results_or_full_coverage(self):
        from modules.insights.poc.devlake.evidence_chain import attempts_and_missing

        report = attempts_and_missing(self.run, [self.selected, self.missing], [self.failed, self.passed])
        self.assertEqual(report["completeness_percent"], 50)
        self.assertEqual(report["acceptance"], "not_assessed")
        self.assertEqual(attempts_and_missing(self.run, [self.selected], [])["completeness_percent"], 0)
        optional = {**self.missing, "required": False}
        self.assertEqual(attempts_and_missing(self.run, [self.selected, optional],
                                              [self.failed, self.passed])["completeness_percent"], 50)
        retry_gap = attempts_and_missing(self.run, [self.selected], [self.passed])
        self.assertEqual(retry_gap["instances"][0]["missing_attempts"], [1])
        self.assertIsNone(retry_gap["completeness_percent"])
        unknown = {**self.run, "selection_status": "unknown", "selection_snapshot": None}
        self.assertIsNone(attempts_and_missing(unknown, [self.selected], [self.passed])["completeness_percent"])
        with self.assertRaisesRegex(ValueError, "duplicates an attempt"):
            attempts_and_missing(self.run, [self.selected], [self.passed, self.passed])
        with self.assertRaisesRegex(ValueError, "unselected"):
            attempts_and_missing(self.run, [self.selected], [self.passed, {
                **self.failed, "run_test_ref": {**self.failed["run_test_ref"], "record_id": "not-selected"}}])


class OfflineChainSelectorTest(unittest.TestCase):
    def setUp(self):
        self.story = json.loads((FIXTURES / "story_revision.json").read_text())
        self.criterion = json.loads((FIXTURES / "acceptance_criterion.json").read_text())
        self.test = json.loads((FIXTURES / "test_definition.json").read_text())
        self.instance = json.loads((FIXTURES / "run_test.json").read_text())
        self.run = json.loads((FIXTURES / "test_run.json").read_text())
        self.result = json.loads((FIXTURES / "test_result.json").read_text())
        self.artifact = ExactOfflineLinkTest()
        self.artifact.setUp()
        self.story_ref = self.criterion["story_ref"]
        self.run_ref = self.instance["run_ref"]

    def authority(self, reference):
        return {"realm_id": "controlled-realm", "visibility": "private", "policy_ref": "authority-policy",
                "policy_revision": 2}

    def capture(self, record, resolve_reference=None):
        source_bytes = json.dumps(record, sort_keys=True).encode()
        snapshot = {"artifact_id": record["source_event_id"], "version_id": record["record_revision"],
                    "sha256": hashlib.sha256(source_bytes).hexdigest()}
        if record["record_type"] == "story_revision":
            record = {**record, "source_snapshot": snapshot}
        return capture_record(record, snapshot, source_bytes, resolve_reference or self.authority)

    def test_bounded_readonly_selector_shows_edges_and_missing_instance(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        missing = copy.deepcopy(self.instance)
        missing.update(record_id="RUN-001/TEST-001/missing", instance_id="missing",
                       source_event_id="RUN-001/TEST-001/missing:revision-1")
        captures = [self.capture(record) for record in (self.story, self.criterion, self.test,
                                                         self.instance, missing, self.run, self.result)]
        invoked = []

        def authorized_read(story_ref, criterion_key, run_ref):
            invoked.append((story_ref, criterion_key, run_ref))
            return captures

        report = read_evidence_chain(authorized_read, self.story_ref, "AC-001", self.run_ref,
                                     self.artifact.artifact, self.artifact.receipt, resolve_reference=self.authority)
        self.assertEqual(invoked, [(self.story_ref, "AC-001", self.run_ref)])
        self.assertEqual(report["audience"], "private")
        self.assertEqual(len(report["revisions"]), 1)
        self.assertEqual(report["revisions"][0]["coverage"]["completeness_percent"], 50)
        self.assertEqual(report["revisions"][0]["links"][0]["delivery"]["status"], "matched_input")
        self.assertIn("planned_result_missing", [gap["reason"] for gap in report["gaps"]])
        self.assertEqual(report["revisions"][0]["assessments"], [])
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            read_evidence_chain(authorized_read, self.story_ref, "AC-001", self.run_ref,
                                requested_audience="shared", resolve_reference=self.authority)
        forged = copy.deepcopy(captures)
        forged[0]["audience"] = "shared"
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            read_evidence_chain(lambda *args: forged, self.story_ref, "AC-001", self.run_ref,
                                requested_audience="shared", resolve_reference=self.authority)

    def test_shared_projection_authorizes_artifact_and_receipt_dependencies(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        shared_policy = {**self.authority(None), "visibility": "shared"}
        captures = [self.capture(record, lambda reference: shared_policy) for record in
                    (self.story, self.criterion, self.test, self.instance, self.run, self.result)]
        delivery_refs = (self.artifact.artifact["artifact_ref"], self.artifact.receipt["source_snapshot"])
        for restricted_ref in delivery_refs:
            for policy in ({**shared_policy, "visibility": "private"}, None,
                           {**shared_policy, "visibility": "unknown"},
                           {**shared_policy, "realm_id": "another-realm"},
                           {**shared_policy, "policy_revision": 0}):
                with self.subTest(reference=restricted_ref, policy=policy):
                    resolved = []

                    def authority(reference):
                        resolved.append(reference)
                        return policy if reference == restricted_ref else shared_policy

                    with self.assertRaisesRegex(ValueError, "private dependency|authority"):
                        read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                            self.artifact.artifact, self.artifact.receipt,
                                            requested_audience="shared", resolve_reference=authority)
                    self.assertIn(restricted_ref, resolved)

        resolved = []

        def shared_authority(reference):
            resolved.append(reference)
            return shared_policy

        report = read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                    self.artifact.artifact, self.artifact.receipt,
                                    requested_audience="shared", resolve_reference=shared_authority)
        self.assertEqual(report["audience"], "shared")
        self.assertEqual(report["revisions"][0]["links"][0]["delivery"]["status"], "matched_input")
        self.assertEqual([item["reference"] for item in report["dependencies"]], resolved)
        for reference in delivery_refs:
            self.assertIn(reference, resolved)
        for candidate in captures:
            for dependency in candidate["dependencies"]:
                self.assertIn(dependency["reference"], resolved)
        private_report = read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                            self.artifact.artifact, self.artifact.receipt,
                                            resolve_reference=self.authority)
        self.assertEqual(private_report["audience"], "private")
        self.assertTrue(all(item["policy"]["visibility"] == "private"
                            for item in private_report["dependencies"]))

    def test_selector_requires_trusted_resolver_and_exact_delivery_references(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        captures = [self.capture(record) for record in
                    (self.story, self.criterion, self.test, self.instance, self.run, self.result)]
        with self.assertRaisesRegex(ValueError, "authorized read"):
            read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                self.artifact.artifact, self.artifact.receipt, resolve_reference=None)
        for field, value in (("version_id", "latest"), ("sha256", "not-a-digest"), ("artifact_id", "")):
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.artifact.receipt)
                receipt["source_snapshot"][field] = value
                with self.assertRaisesRegex(ValueError, "exact artifact and receipt"):
                    read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                        self.artifact.artifact, receipt, resolve_reference=self.authority)

    def test_unknown_requirement_binding_preserves_partial_chain(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        self.instance["requirement_binding"] = "unknown"
        failed = {**self.result, "outcome": "failed"}
        passed = {**self.result, "attempt": 2, "outcome": "passed",
                  "record_id": self.result["record_id"] + "/2",
                  "source_event_id": self.result["source_event_id"] + "/2"}
        captures = [self.capture(record) for record in
                    (self.story, self.criterion, self.test, self.instance, self.run, failed, passed)]
        report = read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                    self.artifact.artifact, self.artifact.receipt, resolve_reference=self.authority)
        revision = report["revisions"][0]
        self.assertEqual(revision["links"], [])
        self.assertEqual(revision["assessment_status"], "unassessed")
        self.assertIsNone(revision["coverage"]["completeness_percent"])
        instance = revision["coverage"]["instances"][0]
        self.assertEqual(instance["requirement_binding"], "unknown")
        self.assertEqual([(attempt["attempt"], attempt["outcome"]) for attempt in instance["attempts"]],
                         [(1, "failed"), (2, "passed")])
        self.assertIn({"criterion_ref": revision["criterion_ref"], "instance_ref": instance["instance_ref"],
                       "reason": "requirement_binding_unknown"}, report["gaps"])
        for field, value in (("source_revision", "different-code"),
                             ("artifact_ref", {**self.artifact.receipt["artifact_ref"], "sha256": "d" * 64})):
            with self.subTest(field=field):
                receipt = {**self.artifact.receipt, field: value}
                with self.assertRaisesRegex(ValueError, "dev delivery digest or tested code"):
                    read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                        self.artifact.artifact, receipt, resolve_reference=self.authority)

    def test_selector_refuses_cross_scope_and_reports_missing_source(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        captures = [self.capture(record) for record in (self.criterion, self.instance, self.run)]
        report = read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                    resolve_reference=self.authority)
        self.assertIn("story_source_missing", [gap["reason"] for gap in report["gaps"]])
        self.assertIn("test_or_artifact_missing", [gap["reason"] for gap in report["gaps"]])
        outsider = self.capture(self.test)
        outsider["record"]["tenant_id"] = "another-tenant"
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            read_evidence_chain(lambda *args: captures + [outsider], self.story_ref, "AC-001", self.run_ref,
                                resolve_reference=self.authority)

    def test_requirement_change(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        second_story = copy.deepcopy(self.story)
        second_story.update(record_revision="revision-2", source_event_id="issue-42:revision-2",
                            event_time="2026-10-11T09:59:00Z", brief="Invitation needs a verified domain.")
        second_criterion = copy.deepcopy(self.criterion)
        second_criterion.update(record_revision="revision-2", source_event_id="issue-42/AC-001:revision-2",
                                event_time="2026-10-11T10:00:00Z", text="A verified domain is required.")
        second_criterion["story_ref"]["record_revision"] = "revision-2"
        assessment = json.loads((FIXTURES / "acceptance_assessment.json").read_text())
        captures = [self.capture(record) for record in (self.story, self.criterion, self.instance, self.run,
                                                         self.test, self.result, assessment,
                                                         second_story, second_criterion)]
        report = read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                     self.artifact.artifact, self.artifact.receipt, resolve_reference=self.authority)
        first, second = report["revisions"]
        self.assertEqual([entry["criterion_ref"]["record_revision"] for entry in report["revisions"]],
                         ["revision-1", "revision-2"])
        self.assertNotEqual(first["source_snapshot"]["sha256"], second["source_snapshot"]["sha256"])
        self.assertEqual(first["assessment_status"], "recorded")
        self.assertEqual(first["assessments"][0]["criterion_ref"], first["criterion_ref"])
        self.assertEqual(first["assessments"][0]["assessor_id"], "evaluator-example")
        self.assertEqual(second["assessment_status"], "unassessed")
        self.assertEqual(second["assessments"], [])
        self.assertEqual(second["links"], [])
        self.assertIsNone(second["coverage"])
        self.assertIn({"criterion_ref": second["criterion_ref"], "reason": "selection_missing"}, report["gaps"])

        conflicting = copy.deepcopy(captures)
        conflicting_criterion = copy.deepcopy(conflicting[1])
        conflicting_criterion["source_snapshot"]["version_id"] = "different-version"
        conflicting.append(conflicting_criterion)
        with self.assertRaisesRegex(ValueError, "conflicting bytes"):
            read_evidence_chain(lambda *args: conflicting, self.story_ref, "AC-001", self.run_ref,
                                resolve_reference=self.authority)

    def test_attempts_and_missing(self):
        from modules.insights.poc.devlake.evidence_chain import read_evidence_chain

        failed = copy.deepcopy(self.result)
        failed["outcome"] = "failed"
        passed = copy.deepcopy(self.result)
        passed.update(attempt=2, outcome="passed", record_id=self.result["record_id"] + "/2",
                      source_event_id=self.result["source_event_id"] + "/2")
        missing = copy.deepcopy(self.instance)
        missing.update(record_id=self.instance["record_id"] + "/missing", instance_id="missing",
                       source_event_id=self.instance["source_event_id"] + "/missing")
        captures = [self.capture(record) for record in (self.story, self.criterion, self.test, self.instance,
                                                         missing, self.run, passed, failed)]
        report = read_evidence_chain(lambda *args: captures, self.story_ref, "AC-001", self.run_ref,
                                     self.artifact.artifact, self.artifact.receipt, resolve_reference=self.authority)
        revision = report["revisions"][0]
        self.assertEqual([(link["attempt"], link["outcome"]) for link in
                          sorted(revision["links"], key=lambda link: link["attempt"])],
                         [(1, "failed"), (2, "passed")])
        selected = {entry["instance_ref"]["record_id"]: entry for entry in revision["coverage"]["instances"]}
        self.assertEqual(selected[missing["record_id"]]["status"], "missing")
        self.assertEqual(selected[missing["record_id"]]["attempts"], [])
        self.assertEqual(revision["coverage"]["completeness_percent"], 50)
        self.assertEqual(revision["coverage"]["acceptance"], "not_assessed")
        self.assertEqual(revision["assessment_status"], "unassessed")
        self.assertIn({"criterion_ref": revision["criterion_ref"],
                       "instance_ref": {field: missing[field] for field in
                                        ("tenant_id", "project_id", "source_connection_id",
                                         "record_type", "record_id", "record_revision")},
                       "reason": "planned_result_missing"}, report["gaps"])

        no_selection = copy.deepcopy(captures)
        for candidate in no_selection:
            if candidate["record"]["record_type"] == "test_run":
                candidate["record"]["selection_status"] = "unknown"
                candidate["record"]["selection_snapshot"] = None
                candidate["record_sha256"] = hashlib.sha256(json.dumps(candidate["record"], sort_keys=True,
                                                                    separators=(",", ":")).encode()).hexdigest()
                break
        unknown_report = read_evidence_chain(lambda *args: no_selection, self.story_ref, "AC-001",
                                             self.run_ref, self.artifact.artifact, self.artifact.receipt,
                                             resolve_reference=self.authority)
        self.assertIsNone(unknown_report["revisions"][0]["coverage"]["completeness_percent"])
        self.assertEqual(unknown_report["revisions"][0]["links"], [])
        self.assertIn("selection_snapshot_missing", [gap["reason"] for gap in unknown_report["gaps"]])


class NativeRequirementMapperTest(unittest.TestCase):
    def setUp(self):
        self.issue = {"node_id": "issue-42", "title": "Invite a member", "state": "open",
                      "html_url": "https://github.example.com/example/project/issues/42",
                      "updated_at": "2026-10-10T10:00:00Z",
                      "body": "## Validation\n| ID | Given | Expected |\n|---|---|---|\n"
                              "| AC-001 | Admin invites member | Invitation is delivered. |"}
        self.event = {"tenant_id": "tenant-example", "project_id": "project-example",
                      "source_connection_id": "connection-example", "source_event_id": "issue-edit-1",
                      "record_revision": "revision-1", "event_time": "2026-10-10T10:00:00Z",
                      "ingested_at": "2026-10-10T10:01:00Z", "source_action": "edited"}

    def capture(self):
        from modules.insights.poc.devlake.evidence_chain import map_github_requirement_revision

        source_bytes = json.dumps(self.issue, sort_keys=True).encode()
        snapshot = {"artifact_id": "issue-snapshot", "version_id": self.event["record_revision"],
                    "sha256": hashlib.sha256(source_bytes).hexdigest()}
        resolved = []

        def authority(reference):
            resolved.append(reference)
            return {"realm_id": "controlled-realm", "visibility": "private",
                    "policy_ref": "source-policy", "policy_revision": 2}

        captures = map_github_requirement_revision(source_bytes, snapshot, self.event, "AC-001", authority)
        return captures, resolved

    def test_pinned_native_source_revision_and_criterion_bytes(self):
        from modules.insights.poc.devlake.evidence_chain import criterion_history

        first, refs = self.capture()
        self.assertIn(first[0]["reference"], refs)
        self.assertIn(first[1]["reference"], refs)
        self.assertEqual(first[1]["record"]["story_ref"], first[0]["reference"])
        self.assertEqual(first[1]["record"]["text"], "Invitation is delivered.")
        self.assertEqual(first[0]["audience"], "private")
        self.issue["body"] = self.issue["body"].replace("Invitation is delivered.",
                                                        "Invitation needs a verified domain.")
        self.issue["updated_at"] = "2026-10-11T10:00:00Z"
        self.event.update(source_event_id="issue-edit-2", record_revision="revision-2",
                          event_time="2026-10-11T10:00:00Z")
        second, _ = self.capture()
        history = criterion_history([*first, *second], first[0]["reference"], "AC-001")
        self.assertEqual([item["criterion"]["record_revision"] for item in history],
                         ["revision-1", "revision-2"])
        self.assertNotEqual(history[0]["source_snapshot"]["sha256"],
                            history[1]["source_snapshot"]["sha256"])
        self.assertEqual(history[1]["criterion"]["text"], "Invitation needs a verified domain.")

    def test_missing_or_ambiguous_native_history_never_invents_criterion(self):
        from modules.insights.poc.devlake.evidence_chain import map_github_requirement_revision, criterion_history

        self.event["record_revision"] = "revision-2"
        only_new, _ = self.capture()
        history = criterion_history(only_new, only_new[0]["reference"], "AC-001")
        self.assertEqual([item["criterion"]["record_revision"] for item in history], ["revision-2"])
        self.issue["body"] = self.issue["body"].replace("AC-001", "AC-OTHER")
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            self.capture()
        self.issue["body"] = self.issue["body"].replace("AC-OTHER", "AC-001")
        self.issue["body"] += "\n| AC-001 | Duplicate | Different expected result. |"
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            self.capture()
        wrong_snapshot = {"artifact_id": "issue-snapshot", "version_id": "revision-2",
                          "sha256": "a" * 64}
        self.issue["body"] = self.issue["body"].split("\n| AC-001 | Duplicate")[0]
        source_bytes = json.dumps(self.issue, sort_keys=True).encode()
        with self.assertRaisesRegex(ValueError, "exact source snapshot"):
            map_github_requirement_revision(source_bytes, wrong_snapshot, self.event, "AC-001",
                                            lambda reference: None)


class NativeCIMapperTest(unittest.TestCase):
    def setUp(self):
        run = json.loads((FIXTURES / "test_run.json").read_text())
        definition = json.loads((FIXTURES / "test_definition.json").read_text())
        selected = json.loads((FIXTURES / "run_test.json").read_text())
        result = json.loads((FIXTURES / "test_result.json").read_text())
        self.event = {"tenant_id": run["tenant_id"], "project_id": run["project_id"],
                      "source_connection_id": run["source_connection_id"],
                      "source_event_id": "ci-run-event-1", "record_revision": "ci-run-revision-1",
                      "event_time": run["event_time"], "ingested_at": run["ingested_at"]}
        self.native_run = {field: run[field] for field in
                           ("source_run_id", "source_attempt", "source_revision", "environment",
                            "execution_status", "started_at", "ended_at", "catalog_snapshot",
                            "requirements_snapshot")}
        self.native_run["selection_complete"] = True
        test = {"id": definition["record_id"], "revision": definition["record_revision"]}
        test.update({field: definition[field] for field in
                     ("name", "source_path", "selector", "framework", "layer", "source_revision",
                      "source_event_id", "event_time", "ingested_at")})
        instance = {"test": test, "instance_id": selected["instance_id"],
                    "parameter_digest": selected["parameter_digest"], "required": True,
                    "scenario_refs": selected["scenario_refs"], "criterion_refs": selected["criterion_refs"],
                    "requirement_binding": "complete"}
        self.selection = {"instances": [instance, {**instance, "instance_id": "not-run"}]}
        receipt = {"source_run_id": run["source_run_id"], "source_attempt": run["source_attempt"],
                   "test_id": test["id"], "instance_id": selected["instance_id"],
                   "source_event_id": "ci-attempt-event-1", "attempt": 1, "outcome": "failed",
                   "started_at": result["started_at"], "ended_at": result["ended_at"],
                   "duration_ms": result["duration_ms"], "failure_category": "assertion",
                   "artifact_refs": result["artifact_refs"]}
        self.receipts = [receipt, {**receipt, "source_event_id": "ci-attempt-event-2",
                                   "attempt": 2, "outcome": "passed", "failure_category": None}]

    def map(self, selection=True):
        from modules.insights.poc.devlake.evidence_chain import map_native_ci_run

        def pinned(payload, artifact_id):
            contents = json.dumps(payload, sort_keys=True).encode()
            reference = {"artifact_id": artifact_id, "version_id": self.event["record_revision"],
                         "sha256": hashlib.sha256(contents).hexdigest()}
            return contents, reference

        run_bytes, run_snapshot = pinned(self.native_run, "ci-run")
        selection_bytes, selection_snapshot = pinned(self.selection, "ci-selection") if selection else (None, None)
        receipts = [pinned(receipt, receipt["source_event_id"]) for receipt in self.receipts]
        return map_native_ci_run(run_bytes, run_snapshot, selection_bytes, selection_snapshot,
                                 receipts, self.event, lambda reference: {
                                     "realm_id": "controlled-realm", "visibility": "private",
                                     "policy_ref": "ci-policy", "policy_revision": 3})

    def test_pinned_selection_attempts_and_missing_result(self):
        from modules.insights.poc.devlake.evidence_chain import attempts_and_missing

        captured = self.map()
        records = [candidate["record"] for candidate in captured]
        runs = [record for record in records if record["record_type"] == "test_run"]
        definitions = [record for record in records if record["record_type"] == "test_definition"]
        instances = [record for record in records if record["record_type"] == "run_test"]
        results = [record for record in records if record["record_type"] == "test_result"]
        self.assertEqual((len(runs), len(definitions), len(instances), len(results)), (1, 1, 2, 2))
        self.assertEqual([record["attempt"] for record in results], [1, 2])
        self.assertEqual([record["outcome"] for record in results], ["failed", "passed"])
        self.assertEqual(definitions[0]["source_revision"], runs[0]["source_revision"])
        self.assertEqual([candidate["audience"] for candidate in captured], ["private"] * len(captured))
        coverage = attempts_and_missing(runs[0], instances, results)
        self.assertEqual(coverage["completeness_percent"], 50)
        self.assertEqual([item["status"] for item in coverage["instances"]], ["observed", "missing"])
        self.assertEqual(coverage["acceptance"], "not_assessed")

    def test_rerun_stays_distinct_and_green_summary_does_not_invent_tests(self):
        from modules.insights.poc.devlake.evidence_chain import attempts_and_missing

        first = self.map()
        self.native_run["source_attempt"] = 2
        self.event.update(source_event_id="ci-run-event-2", record_revision="ci-run-revision-2")
        self.receipts = []
        rerun = self.map()
        first_run = first[0]["record"]
        second_run = rerun[0]["record"]
        self.assertNotEqual(first[0]["reference"], rerun[0]["reference"])
        self.assertNotEqual(first_run["source_attempt"], second_run["source_attempt"])
        records = [candidate["record"] for candidate in rerun]
        self.assertEqual(len([record for record in records if record["record_type"] == "test_result"]), 0)
        self.assertEqual(attempts_and_missing(second_run, [record for record in records
                                                           if record["record_type"] == "run_test"],
                                              [])["completeness_percent"], 0)
        self.native_run["selection_complete"] = False
        unknown = self.map()
        self.assertEqual(unknown[0]["record"]["selection_status"], "unknown")
        self.assertIsNone(attempts_and_missing(unknown[0]["record"], [candidate["record"] for candidate in unknown
                                                                    if candidate["record"]["record_type"] == "run_test"],
                                               [])["completeness_percent"])
        self.assertGreater(len(first), len(self.map(selection=False)))

    def test_unselected_or_duplicate_native_attempts_refuse_capture(self):
        self.receipts.append({**self.receipts[0], "test_id": "not-selected",
                              "source_event_id": "ci-attempt-event-3"})
        with self.assertRaisesRegex(ValueError, "exact selected"):
            self.map()
        self.receipts[-1] = {**self.receipts[0], "source_event_id": "ci-attempt-event-3"}
        with self.assertRaisesRegex(ValueError, "duplicate native retry"):
            self.map()
        self.receipts = []
        self.selection["instances"][0]["test"]["source_revision"] = "old-source"
        with self.assertRaisesRegex(ValueError, "tested code"):
            self.map()

    def test_definition_bytes_stay_stable_across_run_events(self):
        first = self.map()
        self.native_run["source_attempt"] = 2
        self.event.update(source_event_id="ci-run-event-2", record_revision="ci-run-revision-2",
                          event_time="2026-10-12T11:00:00Z", ingested_at="2026-10-12T11:01:00Z")
        self.receipts = [{**receipt, "source_attempt": 2,
                          "source_event_id": receipt["source_event_id"] + "/rerun"} for receipt in self.receipts]
        rerun = self.map()
        first_definition = next(item for item in first if item["record"]["record_type"] == "test_definition")
        rerun_definition = next(item for item in rerun if item["record"]["record_type"] == "test_definition")
        self.assertEqual(first_definition["reference"], rerun_definition["reference"])
        self.assertEqual(first_definition["record"], rerun_definition["record"])
        self.assertEqual(first_definition["record_sha256"], rerun_definition["record_sha256"])
        native_test = self.selection["instances"][0]["test"]
        for field in ("source_event_id", "event_time", "ingested_at"):
            self.assertEqual(rerun_definition["record"][field], native_test[field])
        for record_type in ("run_test", "test_result"):
            first_record = next(item for item in first if item["record"]["record_type"] == record_type)
            rerun_record = next(item for item in rerun if item["record"]["record_type"] == record_type)
            self.assertNotEqual(first_record["reference"], rerun_record["reference"])
            self.assertNotEqual(first_record["record"]["source_event_id"], rerun_record["record"]["source_event_id"])
            self.assertEqual(rerun_record["record"]["event_time"], self.event["event_time"])

    def test_definition_requires_its_own_revision_provenance(self):
        for field in ("source_event_id", "event_time", "ingested_at"):
            with self.subTest(field=field):
                self.setUp()
                del self.selection["instances"][0]["test"][field]
                with self.assertRaisesRegex(ValueError, "invalid v2 record"):
                    self.map()

    def test_empty_selection_still_requires_exact_snapshot_bytes(self):
        from modules.insights.poc.devlake.evidence_chain import map_native_ci_run

        run_bytes = json.dumps(self.native_run, sort_keys=True).encode()
        run_snapshot = {"artifact_id": "ci-run", "version_id": "run-1",
                        "sha256": hashlib.sha256(run_bytes).hexdigest()}
        selection_bytes = b'{"instances": []}'
        selection_snapshot = {"artifact_id": "ci-selection", "version_id": "selection-1",
                              "sha256": hashlib.sha256(selection_bytes).hexdigest()}
        resolved = []

        def authority(reference):
            resolved.append(reference)
            return {"realm_id": "controlled-realm", "visibility": "private",
                    "policy_ref": "ci-policy", "policy_revision": 3}

        captured = map_native_ci_run(run_bytes, run_snapshot, selection_bytes, selection_snapshot,
                                     [], self.event, authority)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["record"]["selection_status"], "complete")
        self.assertEqual(captured[0]["record"]["selection_snapshot"], selection_snapshot)
        self.assertIn(selection_snapshot, resolved)
        for field, value in (("sha256", "0" * 64), ("version_id", "latest"), ("artifact_id", "")):
            with self.subTest(field=field):
                resolved.clear()
                with self.assertRaisesRegex(ValueError, "exact source snapshot"):
                    map_native_ci_run(run_bytes, run_snapshot, selection_bytes, {**selection_snapshot, field: value},
                                      [], self.event, authority)
                self.assertEqual(resolved, [])
