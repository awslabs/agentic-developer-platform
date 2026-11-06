"""Independent examples for contract review and asynchronous monitoring.

These exercise schemas and transport examples, not deployed handlers/providers.
Semantic authorization, retention and stale-write enforcement remain host tests.
"""

import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent
RECORD = {
    "tenant_id": "example-team", "project_id": "project-example",
    "source_connection_id": "connection-example", "record_type": "repository_commit",
    "record_id": "repository-example", "record_revision": "a" * 40,
}
DOCUMENT = {**RECORD, "record_type": "document", "record_id": "architecture-example", "record_revision": "doc-version-1"}
JOB = {**RECORD, "record_type": "code_review_job", "record_id": "job-example", "record_revision": "job-version-1"}
SCOPE = {
    "tenant_id": "example-team", "product_id": "product-example", "project_id": "project-example",
    "relationship_revision": "relationship-1", "owner_id": "owner-example", "visibility": "only_me",
}
TRIGGER = {
    "trigger_id": "nightly-code", "kind": "schedule", "enabled": True,
    "binding_id": "binding-code", "capability": "code_review",
    "source_refs": [RECORD], "schedule": "rate(1 day)", "debounce_seconds": 0,
}
SETTINGS = {
    "profile_ref": {**RECORD, "record_type": "product_profile", "record_revision": "profile-1"},
    "source_refs": [RECORD, DOCUMENT], "capabilities": ["code_review", "threat_model"],
    "enabled": True, "visibility": "only_me", "exclusions": [], "overrides": [],
    "purpose": "Example security assessment", "classification": "internal",
    "triggers": [TRIGGER, {**TRIGGER, "trigger_id": "nightly-model", "binding_id": "binding-model",
                           "capability": "threat_model", "source_refs": [DOCUMENT]}],
}
CONFIGURATION = {
    "configuration_id": "configuration-example", "revision": 7, "scope": SCOPE,
    "state": "ready", "settings": SETTINGS, "inherited_fields": [], "gaps": [],
    "handoff_state": "linked", "hash": "1" * 64,
}
ARTIFACT = {"artifact_id": "artifact-example", "version": "version-1", "sha256": "2" * 64,
            "media_type": "application/json", "bytes": 128}


def setup_intent(request):
    return {
        "operation_id": "operation-example", "scope": SCOPE,
        "configuration_id": "configuration-example", "request": request,
        "authorization": {"actor_id": "owner-example", "delegation_id": "delegation-example",
                          "delegation_revision": 3, "authorized_at": "2026-01-01T00:00:00Z"},
        "intent_hash": "3" * 64,
    }


class ReviewContractChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "schemas.json").read_text())
        cls.tools = {tool["x-http-operation"]: tool for tool in json.loads((ROOT / "mcp-tools.json").read_text())}

    def valid(self, name, value, expected=True):
        schema = {**self.schema, "$ref": "#/$defs/" + name}
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
        self.assertEqual(not errors, expected, [error.message for error in errors])

    def tool_valid(self, operation, direction, value, expected=True):
        validator = Draft202012Validator(self.tools[operation][direction + "Schema"], format_checker=FormatChecker())
        errors = list(validator.iter_errors(value))
        self.assertEqual(not errors, expected, [error.message for error in errors])

    def test_finops_trigger_does_not_weaken_source_only_service_contracts(self):
        trigger = {"trigger_id": "inquiry-example", "kind": "manual", "enabled": True,
                   "capability": "cost_inquiry", "binding_id": "binding-example", "source_refs": []}
        self.valid("FinopsConfigurationInput", {"service_id": "finops", "finops": {
            "agent_mode": "adopt", "native_agent_ref": "agent-example", "native_agent_display_name": "Example Agent"},
            "triggers": [trigger]})
        self.valid("FinopsTrigger", {**trigger, "capability": "code_review"}, False)
        self.valid("FinopsTrigger", {key: value for key, value in trigger.items() if key != "binding_id"}, False)
        self.valid("Trigger", {**trigger, "capability": "code_review", "debounce_seconds": 0}, False)

    def test_submit_run_service_variants_remain_disjoint(self):
        examples = json.loads((ROOT / "examples.json").read_text())
        runs = [example["value"] for example in examples
                if example["schema"] == "RunInput" and example["valid"]]
        security = next(run for run in runs if "service_id" not in run)
        runs.append({**security, "service_id": "security"})
        for run in runs:
            original_service = run.get("service_id", "security")
            arguments = {
                "product_id": SCOPE["product_id"], "project_id": SCOPE["project_id"],
                "service_id": original_service, "input": run,
                "idempotency_key": "inquiry-key-123456",
            }
            with self.subTest(service=original_service, explicit="service_id" in run):
                self.valid("RunInput", run)
                self.tool_valid("submitRun", "input", arguments)
            for service_id in self.schema["$defs"]["ServiceId"]["enum"]:
                if service_id == original_service:
                    continue
                invalid = {**run, "service_id": service_id}
                with self.subTest(original_service=original_service, service=service_id):
                    self.valid("RunInput", invalid, False)
                    self.tool_valid("submitRun", "input", {
                        **arguments, "service_id": service_id, "input": invalid,
                    }, False)

    def test_finops_three_client_configuration_and_cost_result_contract(self):
        choices = {"service_id": "finops", "finops": {"agent_mode": "create"}, "enabled": True}
        self.valid("ConfigurationInput", choices)
        self.tool_valid("saveConfiguration", "input", {
            "product_id": SCOPE["product_id"], "project_id": SCOPE["project_id"],
            "service_id": "finops", "input": choices, "idempotency_key": "configuration-key-123456",
            "if_match": '"config-one"',
        })
        self.valid("ConfigurationInput", {**choices, "finops": {"agent_mode": "create", "secret_access_key": "invalid"}}, False)
        self.valid("ConfigurationInput", {**choices, "capabilities": ["threat_model"]}, False)
        config = {**CONFIGURATION, "settings": {**SETTINGS, **choices, "capabilities": [], "triggers": []}}
        self.valid("Configuration", config)
        self.tool_valid("getConfiguration", "output", {"data": config, "http_status": 200, "etag": '"config-one"'})

        artifact = {"artifact_id": "answer-example", "version": "version-1", "sha256": "2" * 64}
        measure = {"amount": None, "currency": "USD", "estimated": None,
                   "source_refreshed_at": None, "freshness_reason": "not_reported", "coverage": "unavailable"}
        query = {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost",
                 "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}
        result = {"scope": SCOPE, "result_id": "result-example", "revision": 1, "kind": "finops_cost_inquiry",
                  "run_id": "run-example", "query": query, "scope_digest": "3" * 64, "binding_generation": 1,
                  "attribution": "unknown", "attribution_evidence": [],
                  "native": {"status": "outcome_unknown", "conversation_id": None, "turn_id": None,
                             "answer_artifact": artifact, "observed_at": None, "measure": measure},
                  "oracle": {"collection_id": "collection-example", "query_hash": "4" * 64,
                             "observed_at": None, "pages": [artifact], "measure": {
                                 **measure, "amount": "-12.3400", "estimated": False, "coverage": "complete"}},
                  "comparison": {"state": "incomparable", "delta": None, "display_quantum": None,
                                 "reason": "native_unavailable"},
                  "billing_scope": {"account_ids": ["000000000000"], "view_ref": "view-example",
                                    "snapshot_artifact": artifact}}
        self.valid("Result", result)
        self.tool_valid("getResult", "output", {"data": result, "http_status": 200, "etag": '"1"'})
        self.valid("Result", {**result, "coverage": "complete"}, False)

    def test_enabled_trigger_requires_a_complete_target(self):
        self.valid("Trigger", TRIGGER)
        for missing in (("capability",), ("binding_id",), ("capability", "binding_id")):
            self.valid("Trigger", {key: value for key, value in TRIGGER.items() if key not in missing}, False)
        self.valid("Trigger", {**TRIGGER, "source_refs": []}, False)

    def test_disabled_draft_and_schedule_requirements(self):
        draft = {key: value for key, value in TRIGGER.items() if key not in {"capability", "binding_id"}}
        self.valid("Trigger", {**draft, "enabled": False})
        self.valid("Trigger", {key: value for key, value in TRIGGER.items() if key != "schedule"}, False)

    def test_two_capabilities_and_bindings_have_unambiguous_run_inputs(self):
        self.valid("ConfigurationInput", SETTINGS)
        expected_targets = [("code_review", "binding-code", RECORD), ("threat_model", "binding-model", DOCUMENT)]
        for trigger, (capability, binding, source) in zip(SETTINGS["triggers"], expected_targets):
            run = {"configuration_revision": 7, "capability": trigger["capability"],
                   "binding_id": trigger["binding_id"], "sources": trigger["source_refs"]}
            self.valid("RunInput", run)
            self.assertEqual((run["capability"], run["binding_id"], run["sources"]), (capability, binding, [source]))

    def test_unsupported_capability_rejected_at_each_entry(self):
        unsupported = "endpoint_scan"
        self.valid("ConfigurationInput", {**SETTINGS, "capabilities": ["code_review", unsupported]}, False)
        self.valid("Trigger", {**TRIGGER, "capability": unsupported}, False)
        run = {"configuration_revision": 7, "binding_id": "binding-code",
               "capability": unsupported, "sources": [RECORD]}
        self.valid("RunInput", run, False)
        options = {"connections": [], "sources": [], "regions": [],
                   "capabilities": [unsupported], "unavailable_reasons": []}
        self.valid("Options", options, False)
        self.assertEqual(self.schema["$defs"]["Capability"]["enum"], ["threat_model", "code_review"])

    def test_unknown_configuration_and_provider_fields_are_rejected(self):
        run = {"configuration_revision": 7, "binding_id": "binding-code",
               "capability": "code_review", "sources": [RECORD]}
        self.valid("RunInput", run)
        self.valid("RunInput", {**run, "provider_role_arn": "not-a-client-input"}, False)
        self.valid("RunInput", {**run, "sources": [RECORD | {"native_id": "not-a-client-input"}]}, False)
        self.valid("ConfigurationInput", SETTINGS)
        self.valid("ConfigurationInput", {**SETTINGS, "account_wide_requirements": {}}, False)
        self.valid("ConfigurationInput", {**SETTINGS, "triggers": [TRIGGER | {"provider_job_id": "unknown"}]}, False)
        self.valid("BindingInput", {"action": "create", "configuration_revision": 7,
                                    "access_key": "not-a-client-input"}, False)

    def test_security_run_inputs_have_capability_specific_sources(self):
        common = {"configuration_revision": 7, "binding_id": "binding-example"}
        review = {**common, "capability": "code_review", "sources": [RECORD]}
        model = {**common, "capability": "threat_model", "sources": [DOCUMENT]}
        self.valid("RunInput", review)
        self.valid("RunInput", {**review, "pr_base": "base-1", "pr_head": "head-1"})
        self.valid("RunInput", model)
        for invalid in ({**review, "sources": [DOCUMENT]}, {**model, "sources": [RECORD]},
                        {**model, "pr_base": "base-1", "pr_head": "head-1"},
                        {**review, "sources": [RECORD | {"provider_space_id": "unverified"}]}):
            self.valid("RunInput", invalid, False)

    def test_pinned_lifecycle_across_two_assessments_and_publication(self):
        scenarios = {entry["id"]: entry for entry in json.loads((ROOT / "scenarios.json").read_text())}
        first, second = (scenarios[name] for name in ("bounded-assessment", "assessment-after-source-update"))
        for run in (first, second):
            admitted = run["admitted_input"]
            serialized = json.dumps(admitted, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(run["input_hash"], hashlib.sha256(serialized).hexdigest())
            self.assertEqual(run["owner"], "execution_worker")
            self.assertEqual(run["provider_submits"], 1)
            self.assertEqual(run["states"].count("submitting"), 1)
            self.assertFalse(run["binding_deleted"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["binding_id"], second["binding_id"])
        self.assertEqual(first["admitted_input"]["binding_generation"], second["admitted_input"]["binding_generation"])
        self.assertNotEqual(first["admitted_input"]["source_revision"], second["admitted_input"]["source_revision"])
        self.assertNotEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(scenarios["binding-revoked"]["owner"], "lifecycle_api")
        publication = scenarios["publication-recovery"]
        self.assertEqual(publication["run_id"], first["run_id"])
        self.assertEqual(publication["owner"], "publication_worker")
        self.assertEqual(publication["provider_submits"], 0)
        self.assertEqual(publication["states"], ["pending", "publishing", "retry_wait", "publishing", "published"])

    def test_only_typed_profile_fields_can_be_overridden(self):
        self.valid("Override", {"field": "classification", "reason": "Reviewed exception", "value": "internal"})
        self.valid("Override", {"field": "source_refs", "reason": "Approved source", "value": [RECORD]})
        for invalid in ({"field": "organization_requirements", "reason": "Not local", "value": "off"},
                        {"field": "classification", "reason": "Wrong type", "value": [RECORD]},
                        {"field": "region", "reason": "Wrong type", "value": [RECORD]}):
            self.valid("Override", invalid, False)

    def test_source_audited_compatibility_and_package_boundaries(self):
        repository = ROOT.parents[3]
        matrix = json.loads((ROOT / "compatibility.json").read_text())
        self.assertEqual(len({row["id"] for row in matrix}), len(matrix))
        self.assertEqual({row["component"] for row in matrix},
                         {"host", "core", "adapter", "migrations", "build", "infra"})
        required = {"profile", "product", "broker", "task", "core", "adapter", "archive",
                    "sdk-host", "provider-gap", "migrations", "build", "lambda-grant",
                    "dispatch", "router", "cli", "mcp", "release"}
        self.assertEqual({row["id"] for row in matrix}, required)
        for row in matrix:
            with self.subTest(row=row["id"]):
                source_path = repository / row["source"]
                self.assertTrue(source_path.is_file(), source_path)
                self.assertIn(row["evidence"], source_path.read_text())
                self.assertTrue(row["owner"] and row["checkpoint"])
                self.assertNotIn("modules/l5-services/", row["target"])
                self.assertTrue(row["target"].startswith(("modules/L5-services/", "modules/gateway/",
                                                         "docs/l5-services/", ".github/workflows/")))
        self.assertIn("1.43.114", next(row for row in matrix if row["id"] == "sdk-host")["checkpoint"])
        self.assertIn("not an approved SDK", next(row for row in matrix if row["id"] == "sdk-host")["checkpoint"])

    def test_link_intent_retains_each_selected_space_and_config_version(self):
        for space in ("space-alpha", "space-beta"):
            request = {"action": "link", "configuration_revision": 7,
                       "native_resource_ref": {**RECORD, "record_type": "agent_space", "record_id": space}}
            self.valid("BindingInput", request)
            intent = json.loads(json.dumps(setup_intent(request)))
            self.valid("SetupIntent", intent)
            self.assertEqual(intent["request"], request)
            missing = copy.deepcopy(intent)
            del missing["request"]["native_resource_ref"]
            self.valid("SetupIntent", missing, False)

    def test_cleanup_requires_confirmation_generation_and_creation_receipt(self):
        request = {"action": "cleanup", "configuration_revision": 7,
                   "binding_id": "binding-owned", "owned_cleanup_confirmed": True}
        intent = {**setup_intent(request), "binding_generation": 4, "owned_resource_receipt": ARTIFACT}
        self.valid("SetupIntent", intent)
        for missing in ("binding_generation", "owned_resource_receipt", "authorization"):
            self.valid("SetupIntent", {key: value for key, value in intent.items() if key != missing}, False)
        intent["request"] = {key: value for key, value in request.items() if key != "owned_cleanup_confirmed"}
        self.valid("SetupIntent", intent, False)

    def test_status_projection_is_not_setup_intent(self):
        status = {"operation_id": "operation-example", "action": "link", "state": "accepted", "revision": 1, "gaps": []}
        self.valid("Operation", status)
        self.valid("SetupIntent", status, False)

    def test_pinned_source_requests_reject_aliases_and_unbounded_reads(self):
        request = {"source_ref": RECORD, "exclusions": [], "delegation_id": "delegation-example",
                   "delegation_revision": 3, "max_bytes": 1048576, "max_files": 100,
                   "retain_until": "2026-01-02T00:00:00Z"}
        self.valid("SourceSnapshotRequest", request)
        for alias in ("latest", "HEAD", "null"):
            self.valid("SourceSnapshotRequest", {**request, "source_ref": {**RECORD, "record_revision": alias}}, False)
        self.valid("SourceSnapshotRequest", {**request, "max_bytes": 1073741825}, False)
        self.valid("SourceSnapshotRequest", {**request, "max_files": 100001}, False)
        self.valid("SourceSnapshotRequest", {**request, "access_token": "forbidden-field"}, False)

    def test_document_receipt_carries_content_and_manifest_integrity(self):
        content = b"Example architecture document.\n"
        digest = hashlib.sha256(content).hexdigest()
        file = {"source_ref": DOCUMENT, "path": "architecture.txt", "bytes": len(content), "sha256": digest}
        manifest = {"source_ref": DOCUMENT, "files": [file], "exclusions": []}
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        receipt = {"snapshot_id": "snapshot-example", "source_ref": DOCUMENT,
                   "content": {**ARTIFACT, "artifact_id": "document-example", "sha256": digest, "bytes": len(content), "media_type": "text/plain"},
                   "file_manifest": {**ARTIFACT, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)},
                   "file_count": 1, "exclusions": [], "expires_at": "2026-01-02T00:00:00Z"}
        self.valid("SourceFileManifest", manifest)
        self.valid("SourceSnapshot", receipt)
        del receipt["content"]["sha256"]
        self.valid("SourceSnapshot", receipt, False)

    def test_source_read_requires_the_current_work_context(self):
        request = {"delegation_id": "delegation-example", "delegation_revision": 3,
                   "operation_id": "run-example", "attempt_id": "attempt-example", "lease_fence": 2}
        self.valid("SourceReadRequest", request)
        for missing in ("lease_fence", "attempt_id", "delegation_revision"):
            self.valid("SourceReadRequest", {key: value for key, value in request.items() if key != missing}, False)

    def test_mcp_read_edit_and_stale_edit_exchange(self):
        # Handwritten transport examples. The 412 is a specified response, not a
        # claim that this design PR implements a conditional-write handler.
        read = {"data": CONFIGURATION, "http_status": 200, "etag": '"opaque-config-a7"'}
        self.tool_valid("getConfiguration", "output", read)
        edit = {"product_id": "product-example", "project_id": "project-example", "service_id": "security",
                "input": SETTINGS, "idempotency_key": "edit-example", "if_match": read["etag"]}
        self.tool_valid("saveConfiguration", "input", edit)
        saved = {"data": {**CONFIGURATION, "revision": 8}, "http_status": 200, "etag": '"opaque-config-b8"'}
        self.tool_valid("saveConfiguration", "output", saved)
        stale_edit = {**edit, "idempotency_key": "stale-edit-example"}
        self.tool_valid("saveConfiguration", "input", stale_edit)
        self.assertNotEqual(stale_edit["if_match"], saved["etag"])
        self.valid("McpError", {"http_status": 412, "error": {"code": "stale_etag", "message": "Read current version before editing", "retryable": False, "request_id": "request-example"}})
        self.tool_valid("getConfiguration", "output", {"data": CONFIGURATION, "http_status": 200}, False)

    def test_tokens_are_strong_and_returned_for_empty_triage(self):
        for token in ('W/"weak"', "7", '"contains space"'):
            self.valid("ETag", token, False)
        self.tool_valid("listTriage", "output", {"data": {"items": []}, "http_status": 200, "etag": '"empty-triage-head"'})
        self.tool_valid("listTriage", "output", {"data": {"items": []}, "http_status": 200}, False)
        self.tool_valid("refreshConfiguration", "input", {"product_id": "product-example", "project_id": "project-example", "service_id": "security", "input": {"profile_ref": RECORD}, "idempotency_key": "refresh-example"}, False)

    def test_monitoring_defaults_require_resolution_before_admission(self):
        self.valid("MonitoringPolicy", {})
        self.valid("EffectiveMonitoringPolicy", {}, False)
        effective = {"poll_interval_seconds": 600, "poll_grace_seconds": 300}
        self.valid("EffectiveMonitoringPolicy", effective)
        self.valid("ConfigurationInput", {**SETTINGS, "monitoring": effective})
        properties = self.schema["$defs"]["MonitoringPolicy"]["properties"]
        self.assertEqual(properties["poll_interval_seconds"]["default"], 600)
        self.assertEqual(properties["poll_grace_seconds"]["default"], 300)
        self.assertNotIn("default", properties["stall_after_seconds"])

    def test_monitoring_configuration_is_bounded(self):
        self.valid("MonitoringPolicy", {"poll_interval_seconds": 600, "stall_after_seconds": 7200})
        for value in (0, 901, 600.5):
            self.valid("MonitoringPolicy", {"poll_interval_seconds": value}, False)
        self.valid("MonitoringPolicy", {"poll_grace_seconds": 0}, False)
        self.valid("MonitoringPolicy", {"stall_after_seconds": 86401}, False)
        self.valid("MonitoringPolicy", {"restart_stalled_job": True}, False)

    def test_optional_provider_fields_and_unknown_status_survive_observation(self):
        observation = {"observed_at": "2026-01-01T00:10:00Z", "native_job_ref": JOB,
                       "native_status": "IN_PROGRESS", "outcome": "running", "progress_coverage": "unavailable"}
        self.valid("ProviderObservation", observation)
        self.valid("ProviderObservation", {**observation, "native_status": "FUTURE_PROVIDER_STATE", "outcome": "unknown"})
        partial = {**observation, "progress_coverage": "partial",
                   "phases": [{"name": "STATIC_ANALYSIS", "native_status": "IN_PROGRESS"}],
                   "tasks": {"running": 3, "completed": 2, "failed": 1, "stopped": 0, "unknown": 0}}
        self.valid("ProviderObservation", partial)
        self.valid("ProviderObservation", {**partial, "heartbeat_at": "2026-01-01T00:10:00Z"}, False)

    def test_failed_poll_and_native_failure_are_separate_records(self):
        running = {"observed_at": "2026-01-01T00:10:00Z", "native_job_ref": JOB,
                   "native_status": "IN_PROGRESS", "outcome": "running", "progress_coverage": "unavailable"}
        monitoring = {"state": "degraded", "progress_state": "unknown", "poll_attempts": 2,
                      "consecutive_poll_errors": 1, "last_successful_poll_at": "2026-01-01T00:10:00Z",
                      "last_poll_attempt_at": "2026-01-01T00:20:00Z", "next_poll_at": "2026-01-01T00:30:00Z",
                      "last_poll_error": {"code": "transport_timeout"}, "latest_observation": running}
        self.valid("RunMonitoring", monitoring)
        failed = {**running, "native_status": "FAILED", "outcome": "failed", "error": {"code": "CLIENT_ERROR", "message": "Example source access failure"}}
        self.valid("ProviderObservation", failed)
        self.valid("RunMonitoring", {**monitoring, "state": "failed"}, False)
        self.assertIn("failed", self.schema["$defs"]["Run"]["properties"]["provider_state"]["enum"])


if __name__ == "__main__":
    unittest.main()
