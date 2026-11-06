"""Offline checks of the proposed DevOps L5 contract, not provider behavior."""

import csv
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from jsonschema import Draft202012Validator


REPO = Path(__file__).resolve().parents[4]
ROOT = REPO / "docs/l5-services/contracts/v1"
MATRIX = REPO / "docs/l5-services/devopsagnent/capability-matrix.json"
SCHEMA = json.loads((ROOT / "schemas.json").read_text())
sys.path.insert(0, str(ROOT))
from validate import references_in_scope


def record(project="project-example", kind="resource"):
    return {"tenant_id": "tenant-example", "project_id": project,
            "source_connection_id": "connection-example", "record_type": kind,
            "record_id": "record-example", "record_revision": "revision-1"}


def configuration(project="project-example", service="devops"):
    profile = record(project, "profile")
    settings = {"profile_ref": profile, "source_refs": [], "capabilities": ["investigation"],
                "enabled": False, "visibility": "product_team", "exclusions": [],
                "overrides": [], "triggers": [], "service_id": "devops",
                "devops": {"hosting_connection_ref": record(project, "connection"), "region": "example-region-1",
                           "space_mode": "create", "accounts": [{"connection_ref": record(project, "connection"),
                               "purpose": "monitor", "resource_role_ref": record(project, "role"),
                               "environments": [{"environment_ref": record(project, "environment"),
                                   "workload_kind": "aws_resource", "resource_refs": [record(project)]}]}],
                           "integrations": [{"kind": "cloudwatch", "connection_ref": record(project, "connection"),
                               "access_mode": "read", "event_support": False,
                               "resource_refs": [record(project, "telemetry")]}],
                           "access_policy_ref": record(project, "policy"),
                           "authentication_mode": "scoped_iam_federation", "permitted_native_actions": [],
                           "knowledge_policy": {"source_refs": [], "visibility": "product_team",
                                                "publish_results": False, "refresh": "manual"},
                           "trigger_policies": [], "prevention_enabled": False, "retention_days": 30,
                           "browser_launch_policy": "disabled"}}
    if service == "security":
        settings.pop("service_id")
        settings.pop("devops")
        settings["capabilities"] = ["code_review"]
    return settings


def portable_settings(case):
    project = case["project_id"]
    settings = configuration(project)
    settings["capabilities"] = case["capabilities"]
    devops = settings["devops"]
    if "account" not in case["available_inputs"]:
        devops["accounts"] = []
    else:
        environment = devops["accounts"][0]["environments"][0]
        environment["environment_ref"]["record_id"] = case["environment_id"]
        environment["workload_kind"] = case["workload_kind"]
        if case["workload_kind"] == "eks":
            if "cluster_ref" in case["available_inputs"]:
                environment["cluster_ref"] = record(project, "cluster")
            if "namespace" in case["available_inputs"]:
                environment["namespace"] = "namespace-" + case["project_id"]
    devops["integrations"] = []
    for kind in case["integrations"]:
        integration = {"kind": kind, "connection_ref": record(project, "connection"),
                       "access_mode": "read", "event_support": False}
        if kind == "github" and "repository" in case["available_inputs"]:
            integration["repository_refs"] = [record(project, "repository_commit") |
                                              {"record_id": case["repository_id"]}]
        if kind == "cloudwatch" and "telemetry" in case["available_inputs"]:
            integration["resource_refs"] = [record(project, "telemetry")]
        devops["integrations"].append(integration)
    return settings


def valid(name, value):
    return Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{name}"}).is_valid(value)


class DevopsContractChecks(unittest.TestCase):
    def test_manual_investigation_limits_need_no_budget_reference(self):
        limits = {"deadline_seconds": 3600, "max_provider_attempts": 1,
                  "max_polls": 60, "concurrency": 1, "max_cost": "300", "currency": "USD"}
        self.assertTrue(valid("DevopsLimits", limits))
        self.assertFalse(valid("Limits", limits))
        settings = configuration() | {"limits": limits}
        self.assertTrue(valid("ConfigurationInput", settings))
        self.assertFalse(valid("ConfigurationInput", configuration(service="security") | {"limits": limits}))
        self.assertFalse(valid("DevopsLimits", limits | {"policy_ref": {}}))
        self.assertFalse(valid("DevopsLimits", limits | {"deadline_seconds": 0}))

    def test_enabled_service_triggers(self):
        capabilities = {"security": ["threat_model", "code_review"],
                        "devops": ["investigation", "release_readiness", "release_verification", "prevention"]}
        for service, supported in capabilities.items():
            for capability in supported:
                with self.subTest(service=service, capability=capability):
                    settings = configuration(service=service)
                    settings["enabled"] = True
                    settings["capabilities"] = [capability]
                    trigger = {"trigger_id": "trigger-example", "kind": "manual", "enabled": True,
                               "capability": capability, "binding_id": "binding-example",
                               "source_refs": [record()], "debounce_seconds": 0}
                    settings["triggers"] = [trigger]
                    self.assertTrue(valid("Trigger", trigger))
                    self.assertTrue(valid("ConfigurationInput", settings))
                    settings["service_id"] = service
                    self.assertTrue(valid("ConfigurationInput", settings))
                    foreign = "investigation" if service == "security" else "code_review"
                    settings["triggers"] = [trigger | {"capability": foreign}]
                    self.assertFalse(valid("ConfigurationInput", settings))
                    for field in ("capability", "binding_id"):
                        settings["triggers"] = [{key: value for key, value in trigger.items() if key != field}]
                        self.assertFalse(valid("ConfigurationInput", settings))

    def test_complete_service_options(self):
        options = {"connections": [], "sources": [], "regions": [], "unavailable_reasons": [],
                   "service_id": "devops",
                   "capabilities": ["investigation", "release_readiness", "release_verification", "prevention"]}
        self.assertTrue(valid("Options", options))
        self.assertFalse(valid("Options", options | {"capabilities": options["capabilities"] + ["investigation"]}))
        self.assertFalse(valid("Options", options | {"capabilities": ["code_review"]}))
        security = options | {"service_id": "security", "capabilities": ["threat_model", "code_review"]}
        for explicit_service in (False, True):
            with self.subTest(explicit_service=explicit_service):
                control = {key: value for key, value in security.items() if explicit_service or key != "service_id"}
                self.assertTrue(valid("Options", control))
                self.assertFalse(valid("Options", control | {"capabilities": ["investigation"]}))
                self.assertFalse(valid("Options", control | {"capabilities": ["code_review"] * 3}))

    def test_compatible_contracts(self):
        security = configuration(service="security")
        devops = configuration()
        self.assertTrue(valid("ConfigurationInput", security))
        self.assertTrue(valid("ConfigurationInput", devops))
        security_run = {"configuration_revision": 1, "binding_id": "binding-security",
                        "capability": "code_review", "sources": [record(kind="repository_commit")]}
        devops_run = {"configuration_revision": 1, "binding_id": "binding-devops", "service_id": "devops",
                      "capability": "investigation", "devops_input": {"kind": "investigation",
                          "resource_refs": [record()], "telemetry_refs": [record(kind="telemetry")],
                          "question": "What changed?"}}
        self.assertTrue(valid("RunInput", security_run))
        self.assertTrue(valid("RunInput", devops_run))
        generated = json.loads((ROOT / "examples.json").read_text())
        devops_examples = [example for example in generated
                           if example["value"].get("service_id") == "devops"]
        self.assertEqual([example["valid"] for example in devops_examples], [True, False])
        for example in devops_examples:
            self.assertEqual(valid(example["schema"], example["value"]), example["valid"])
        with (ROOT / "operations.tsv").open() as operation_file:
            operations = list(csv.DictReader(operation_file, delimiter="\t"))
        tools = json.loads((ROOT / "mcp-tools.json").read_text())
        paths = json.loads((ROOT / "openapi.json").read_text())["paths"]
        self.assertEqual({entry["operation_id"] for entry in operations},
                         {operation["operationId"] for methods in paths.values() for operation in methods.values()})
        self.assertEqual({entry["operation_id"] for entry in operations if entry["MCP"] != "-"},
                         {tool["x-http-operation"] for tool in tools})
        launch = next(entry for entry in operations if entry["operation_id"] == "launchNativeBrowser")
        self.assertEqual((launch["MCP"], launch["CLI (adp service)"]), ("-", "-"))
        self.assertEqual(launch["output"], "BrowserLaunch")
        event_read = next(entry for entry in operations if entry["operation_id"] == "listBindingEvents")
        self.assertEqual((event_read["output"], event_read["effect"]), ("DevopsEventList", "read"))
        event = {"event_id": "event-example", "scope": {"tenant_id": "tenant-example",
                 "product_id": "product-example", "project_id": "project-example",
                 "relationship_revision": "relationship-example", "owner_id": "owner-example",
                 "visibility": "product_team"}, "binding_id": "binding-devops", "origin": "native_event",
                 "native_event_ref": record(kind="native_event"), "received_at": "2026-01-01T00:00:00Z",
                 "state": "received"}
        self.assertTrue(valid("DevopsEventList", {"items": [event]}))
        self.assertNotIn("admission", SCHEMA["$defs"]["DevopsEvent"]["properties"])
        operation = paths["/v1/products/{product_id}/projects/{project_id}/services/{service_id}/bindings/{binding_id}/events"]["get"]
        self.assertEqual(set(operation["responses"]) - {"200"},
                         {"401", "403", "404", "409", "410", "412", "422", "429", "503"})
        self.assertTrue(valid("Service", {"service_id": "devops", "availability": "unavailable",
                                          "capabilities": ["investigation"], "contract_version": "1.0",
                                          "reason": "provider qualification pending"}))

    def test_lifecycle_contracts(self):
        fixture = json.loads((ROOT / "lifecycle-fixtures.json").read_text())
        self.assertEqual(fixture["scenario"], "SCN-02")
        self.assertEqual(fixture["fixed_clock"], "2026-01-01T00:00:00Z")
        devops = fixture["devops"]
        owners = {"configuration": "configuration_api", "setup": "setup_worker",
                  "native_ingest": "native_event_ingress", "native_observation": "monitoring_worker",
                  "retention": "lifecycle_api", "publication": "publication_worker"}
        self.assertEqual(devops["owners"], owners)
        self.assertEqual(len({item["id"] for item in devops["transitions"]}), len(devops["transitions"]))
        states = {"configuration": "unconfigured", "setup": "draft", "native": "unseen",
                  "asset": "active", "publication": "pending"}
        provider = Mock(side_effect=TimeoutError("response lost"))
        readback = Mock(return_value={"verified": True})
        consumer = Mock(side_effect=[TimeoutError("receipt lost"), "receipt-example"])
        submissions = 0
        publication_attempts = 0
        for item in devops["transitions"]:
            with self.subTest(transition=item["id"]):
                self.assertIn(item["lane"], owners)
                subject = ({"native_ingest": "native", "native_observation": "native",
                            "retention": "asset"}.get(item["lane"], item["lane"]))
                self.assertEqual(states[subject], item["from"])
                if item["id"] == "create_timeout":
                    with self.assertRaises(TimeoutError):
                        provider("operation-example")
                    submissions += 1
                    next_state = "outcome_unknown"
                elif item["id"] == "native_timeout":
                    next_state = "outcome_unknown"
                elif item["lane"] == "publication":
                    publication_attempts += 1
                    try:
                        consumer("result-example")
                    except TimeoutError:
                        next_state = "retry_wait"
                    else:
                        next_state = "published"
                elif item.get("readback"):
                    self.assertTrue(readback(item["id"])["verified"])
                    next_state = "succeeded"
                else:
                    next_state = {"save": "draft", "setup_intent": "accepted",
                                  "native_receipt": "received", "retain_adopted": "retained"}[item["id"]]
                self.assertEqual(next_state, item["to"])
                states[subject] = next_state
                self.assertEqual(item["provider_submits"], int(item["id"] == "create_timeout"))
        self.assertEqual(devops["expected_readback"], {
            "setup_state": states["setup"], "native_state": states["native"],
            "adp_native_admissions": 0, "setup_provider_submissions": submissions,
            "adopted_asset_state": states["asset"], "publication_state": states["publication"],
            "publication_attempts": publication_attempts})
        provider.assert_called_once_with("operation-example")
        self.assertEqual(readback.call_count, 2)
        for entry in devops["snapshots"]:
            with self.subTest(schema=entry["schema"], state=entry["value"].get("state")):
                self.assertEqual(valid(entry["schema"], entry["value"]), entry["valid"])
        native_run = next(item["value"] for item in devops["snapshots"]
                          if item["schema"] == "Run" and item["value"]["state"] == "outcome_unknown")
        self.assertNotIn("admission", native_run)
        self.assertNotIn("manifest_ref", native_run)
        self.assertTrue(references_in_scope(native_run, "tenant-example", "project-example"))

    def test_invalid_lifecycle(self):
        snapshots = json.loads((ROOT / "lifecycle-fixtures.json").read_text())["devops"]["snapshots"]
        originals = {}
        for item in snapshots:
            if item["valid"]:
                originals.setdefault(item["schema"], item["value"])
        operation = originals["Operation"]
        run = originals["Run"]
        result = originals["Result"]
        asset = originals["DevopsAsset"]
        for schema, bad in (
            ("Operation", operation | {"state": "succeeded", "readback_ref": None}),
            ("Operation", operation | {"state": "outcome_unknown", "readback_ref": record()}),
            ("Run", run | {"state": "succeeded", "provider_state": "unknown"}),
            ("Run", run | {"admission": {"fabricated": True}}),
            ("Run", run | {"state": "admitted"}),
            ("Result", result | {"coverage": "complete"}),
            ("Result", result | {"provider_outcome": "completed", "coverage": "complete"}),
            ("Result", result | {"findings_artifact": {"unverified": True}}),
            ("DevopsAsset", asset | {"state": "removed"}),
        ):
            with self.subTest(schema=schema, bad=bad):
                self.assertFalse(valid(schema, bad))

    def test_capability_evidence(self):
        matrix = json.loads(MATRIX.read_text())
        self.assertEqual(matrix["contract_version"], "devops/1.0-r2")
        catalog = matrix["catalog"]
        self.assertEqual((catalog["status"], catalog["module_id"], catalog["feature_ids"]),
                         ("unresolved", None, {}))
        self.assertEqual(catalog["blocking_phase"], "before dependent dispatch")
        self.assertIn("PranavSharma1000", catalog["owner"])
        for source in catalog["checked_sources"]:
            self.assertTrue((REPO / source).is_file(), source)
        gateway_catalog = (REPO / "modules/gateway/src/catalog/models.py").read_text()
        self.assertIn("class PortfolioVersion", gateway_catalog)
        self.assertIn("class InitiativeVersion", gateway_catalog)
        self.assertNotIn("class ModuleVersion", gateway_catalog)
        self.assertNotIn("class FeatureVersion", gateway_catalog)
        pinned_gateway = (REPO / "modules/gateway/pyproject.toml").read_text()
        self.assertIn('"boto3==1.43.111"', pinned_gateway)
        candidate = {row["id"]: row for row in json.loads((ROOT / "compatibility.json").read_text())}["sdk-host"]
        self.assertIn("1.43.114", candidate["checkpoint"])
        self.assertIn("not an approved SDK", candidate["checkpoint"])
        evidence = {item["id"]: item for item in matrix["evidence"]}
        self.assertEqual(len(evidence), len(matrix["evidence"]))
        self.assertTrue(evidence["iam-federation-poc"]["source"].startswith(
            "https://github.com/aws-e/adp/blob/7f82f551d2f1b8aa4ec46a829a217d6c95daf17e/"))
        self.assertIn("rejected", evidence["iam-federation-poc"]["established"])
        self.assertIn("not independently rerun", evidence["iam-federation-poc"]["revision"])
        self.assertIn("permission was denied", evidence["release-readiness-report"]["established"])
        self.assertIn("no new live check", evidence["context-report"]["revision"])
        for item in evidence.values():
            with self.subTest(evidence=item["id"]):
                self.assertTrue(item["owner"] and item["blocking_phase"] and item["not_established"])
                if not item["source"].startswith("https://"):
                    self.assertTrue((REPO / item["source"]).is_file())
        capabilities = {item["id"]: item for item in matrix["capabilities"]}
        self.assertEqual(set(capabilities), {"native_ui", "investigation", "release_readiness",
                                             "release_verification", "prevention", "knowledge"})
        self.assertTrue({"investigation", "release_readiness", "release_verification", "prevention"}
                        <= set(SCHEMA["$defs"]["DevopsCapability"]["enum"]))
        for item in capabilities.values():
            with self.subTest(capability=item["id"]):
                self.assertEqual(item["status"], "blocked_live")
                self.assertTrue(item["owner"] and item["blocking_phase"] and item["requires"])
                self.assertTrue(set(item["evidence_ids"]) <= set(evidence))
        connectors = {item["kind"]: item for item in matrix["connectors"]}
        self.assertEqual(set(connectors), {"github", "cloudwatch", "eks", "aws_resource",
                                           "gitlab", "custom_oidc", "arbitrary_telemetry"})
        self.assertEqual({item["kind"] for item in matrix["connectors"] if item["category"] == "integration"
                          and item["status"] != "unsupported"},
                         set(SCHEMA["$defs"]["DevopsIntegration"]["properties"]["kind"]["enum"]))
        self.assertEqual({item["kind"] for item in matrix["connectors"] if item["category"] == "workload"},
                         set(SCHEMA["$defs"]["DevopsEnvironment"]["properties"]["workload_kind"]["enum"]))

    def test_unqualified_evidence(self):
        matrix = json.loads(MATRIX.read_text())
        self.assertTrue(matrix["catalog"]["missing_mapping"])
        self.assertIsNone(matrix["catalog"]["module_id"])
        self.assertEqual(matrix["catalog"]["feature_ids"], {})
        for connector in matrix["connectors"]:
            with self.subTest(connector=connector["kind"]):
                self.assertIn(connector["status"], {"proposed_unqualified", "unsupported"})
                self.assertTrue(connector["reason"] and connector["owner"])
        for blocked in ("gitlab", "custom_oidc", "arbitrary_telemetry"):
            self.assertEqual(next(item for item in matrix["connectors"] if item["kind"] == blocked)["status"],
                             "unsupported")
        self.assertFalse(valid("ConfigurationInput", configuration() | {
            "devops": configuration()["devops"] | {
                "integrations": [{"kind": "gitlab", "connection_ref": record(),
                                  "access_mode": "read", "event_support": False,
                                  "repository_refs": [record(kind="repository_commit")]}]}}))
        self.assertEqual({item["status"] for item in matrix["capabilities"]}, {"blocked_live"})
        self.assertTrue(all(item["not_established"] for item in matrix["evidence"]))

    def test_portable_contracts(self):
        fixture = json.loads((MATRIX.parent / "portable-fixtures.json").read_text())
        self.assertEqual((fixture["scenario"], fixture["contract_version"]), ("SCN-04", "devops/1.0-r2"))
        self.assertEqual(fixture["fixed_clock"], "2026-01-01T00:00:00Z")
        self.assertEqual(fixture["controls"], {"fault": "source_read_timeout", "max_polls": 3,
                                               "provider_calls": 0,
                                               "cleanup": "discard isolated fixtures; no external resources created"})
        provider = Mock()
        poll_attempts = 0
        cases = fixture["cases"]
        self.assertEqual(len({case["project_id"] for case in cases}), len(cases))
        self.assertGreaterEqual(len({case["product_id"] for case in cases}), 2)
        self.assertEqual(len({case["team_id"] for case in cases}), len(cases))
        self.assertEqual(len({case["environment_id"] for case in cases}), len(cases))
        self.assertEqual(len({case["repository_id"] for case in cases if "repository_id" in case}), 3)
        self.assertEqual({case["workload_kind"] for case in cases}, {"eks", "aws_resource"})
        bindings = {}
        for case in cases:
            with self.subTest(project=case["project_id"]):
                settings = portable_settings(case)
                self.assertTrue(valid("ConfigurationInput", settings))
                self.assertTrue(references_in_scope(settings, case["tenant_id"], case["project_id"]))
                scope = {"tenant_id": case["tenant_id"], "product_id": case["product_id"],
                         "project_id": case["project_id"], "relationship_revision": "relationship-1",
                         "owner_id": case["team_id"], "visibility": "product_team"}
                saved = {"configuration_id": "config-" + case["project_id"], "service_id": "devops",
                         "revision": 1, "scope": scope, "state": "needs_input", "settings": settings,
                         "inherited_fields": [], "gaps": ["provider_unqualified"], "handoff_state": "pending", "hash": "1" * 64}
                self.assertTrue(valid("Configuration", saved))
                for binding_id in case["binding_ids"]:
                    binding = {"binding_id": binding_id, "scope": scope, "service_id": "devops",
                               "configuration_id": saved["configuration_id"], "revision": 1, "generation": 1,
                               "state": "checking", "ownership": "adp_created", "connection_ref": record(case["project_id"], "connection"),
                               "gaps": []}
                    self.assertTrue(valid("Binding", binding))
                    self.assertNotIn(binding_id, bindings)
                    bindings[binding_id] = (case["product_id"], case["project_id"])
        self.assertEqual(bindings["binding-river-api-one"], bindings["binding-river-api-two"])
        self.assertNotEqual(bindings["binding-river-api-one"], bindings["binding-river-worker"])
        self.assertNotEqual(bindings["binding-river-api-one"], bindings["binding-summit-service"])
        selected_product = bindings["binding-river-api-one"][0]
        selected_project = bindings["binding-river-api-one"][1]
        self.assertFalse(bindings["binding-river-worker"] == (selected_product, selected_project))
        self.assertFalse(bindings["binding-summit-service"] == (selected_product, selected_project))
        foreign = portable_settings(cases[0])
        foreign["devops"]["accounts"][0]["environments"][0]["resource_refs"] = [record(cases[1]["project_id"])]
        self.assertTrue(valid("ConfigurationInput", foreign))
        self.assertFalse(references_in_scope(foreign, cases[0]["tenant_id"], cases[0]["project_id"]))
        non_eks_with_cluster = portable_settings(cases[0])
        non_eks_with_cluster["devops"]["accounts"][0]["environments"][0]["cluster_ref"] = record(cases[0]["project_id"], "cluster")
        self.assertFalse(valid("ConfigurationInput", non_eks_with_cluster))
        self.assertEqual(fixture["expected_platform_artifact"],
                         "one shared L5 contract revision; no project-specific platform build")
        self.assertLessEqual(poll_attempts, fixture["controls"]["max_polls"])
        provider.assert_not_called()
        bindings.clear()
        self.assertFalse(bindings)

    def test_admitted_aws_only_investigation(self):
        case = json.loads((MATRIX.parent / "portable-fixtures.json").read_text())["cases"][0]
        project = case["project_id"]
        scope = {"tenant_id": case["tenant_id"], "product_id": case["product_id"],
                 "project_id": project, "relationship_revision": "relationship-1",
                 "owner_id": case["team_id"], "visibility": "product_team"}
        admission = {"service_id": "devops", "scope": scope,
                     "profile_ref": record(project, "profile"), "connection_ref": record(project, "connection"),
                     "delegation_id": "delegation-example", "delegation_version": 1,
                     "binding_generation": 1, "actor_id": case["team_id"],
                     "configuration_hash": "1" * 64, "adapter_revision": "adapter-1",
                     "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 3,
                                "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                                "policy_ref": record(project, "budget_policy")},
                     "visibility": "product_team", "classification": "internal",
                     "causal_origin": "manual-example", "admitted_at": "2026-01-01T00:00:00Z",
                     "expires_at": "2026-01-01T01:00:00Z", "exclusions": [],
                     "monitoring_policy": {"poll_interval_seconds": 60, "poll_grace_seconds": 60},
                     "source_snapshots": []}
        self.assertTrue(valid("Admission", admission))
        self.assertTrue(references_in_scope(admission, case["tenant_id"], project))
        run_input = {"configuration_revision": 1, "binding_id": case["binding_ids"][0],
                     "service_id": "devops", "capability": "investigation",
                     "devops_input": {"kind": "investigation", "resource_refs": [record(project)],
                                      "telemetry_refs": [record(project, "telemetry")],
                                      "question": "What changed?"}}
        run = {"run_id": "run-example", "scope": scope, "revision": 1, "input": run_input,
               "state": "admitted", "provider_state": "not_started", "admission": admission,
               "deadline_at": "2026-01-01T01:00:00Z", "service_id": "devops", "origin": "adp_submitted"}
        self.assertTrue(valid("RunInput", run_input))
        self.assertTrue(valid("Run", run))
        self.assertFalse(valid("Run", run | {"admission": admission | {"service_id": "security"}}))
        self.assertFalse(valid("Run", run | {"input": run_input | {"service_id": "security"}}))
        self.assertFalse(valid("Admission", {key: value for key, value in admission.items()
                                              if key != "service_id"}))

        artifact = {"artifact_id": "evidence-example", "version": "revision-1", "sha256": "2" * 64,
                    "media_type": "application/json", "bytes": 128}
        completed = run | {"state": "succeeded", "provider_state": "completed",
                           "native_job_ref": record(project, "investigation"), "provider_readback_ref": artifact}
        self.assertTrue(valid("Run", completed))
        result = {"result_id": "result-example", "run_id": completed["run_id"], "scope": completed["scope"],
                  "revision": 1, "service_id": "devops", "origin": "adp_submitted",
                  "admission": completed["admission"], "native_job_ref": completed["native_job_ref"],
                  "observed_at": "2026-01-01T00:30:00Z", "coverage": "complete",
                  "coverage_mode": "native_observation", "provider_outcome": "completed",
                  "evidence_ref": completed["provider_readback_ref"],
                  "findings_artifact": artifact | {"artifact_id": "findings-example"},
                  "finding_count": 0, "costs": [{"kind": "unknown", "currency": "USD"}],
                  "publication_id": "publication-example"}
        self.assertTrue(valid("Result", result))
        self.assertNotIn("manifest_ref", result)
        self.assertEqual(result["admission"], admission)
        self.assertTrue(references_in_scope(result, case["tenant_id"], project))
        for field in ("admission", "provider_outcome", "evidence_ref", "service_id"):
            with self.subTest(missing=field):
                self.assertFalse(valid("Result", {key: value for key, value in result.items() if key != field}))
        self.assertFalse(valid("Result", result | {"admission": admission | {"service_id": "security"}}))
        self.assertFalse(valid("Result", result | {"admission": {key: value for key, value in admission.items()
                                                               if key != "service_id"}}))
        self.assertFalse(valid("Result", result | {"provider_outcome": "outcome_unknown"}))
        self.assertFalse(valid("Result", result | {"origin": "native", "native_event_ref": record(project, "event")}))
        foreign = result | {"admission": admission | {"profile_ref": record("other-project", "profile")}}
        self.assertFalse(references_in_scope(foreign, case["tenant_id"], project))

        snapshot = {"snapshot_id": "snapshot-example", "source_ref": record(project, "repository_commit"),
                    "content": artifact, "file_manifest": artifact, "file_count": 1, "exclusions": [],
                    "expires_at": admission["expires_at"]}
        sourced = result | {"admission": admission | {"source_snapshots": [snapshot]}}
        self.assertTrue(valid("Admission", sourced["admission"]))
        self.assertFalse(valid("Result", sourced))
        self.assertTrue(valid("Result", sourced | {"manifest_ref": artifact}))
        security = {key: value for key, value in result.items() if key not in {"admission", "service_id"}}
        for explicit_service in (False, True):
            with self.subTest(security_service=explicit_service):
                control = security | {"service_id": "security"} if explicit_service else security
                self.assertFalse(valid("Result", control))
                self.assertTrue(valid("Result", control | {"manifest_ref": artifact}))

    def test_conditional_readiness(self):
        fixture = json.loads((MATRIX.parent / "portable-fixtures.json").read_text())
        matrix = json.loads(MATRIX.read_text())
        capabilities = {row["id"]: row for row in matrix["capabilities"]}
        for case in fixture["cases"]:
            for capability, expected in case["expected_input_gaps"].items():
                with self.subTest(project=case["project_id"], capability=capability):
                    self.assertIn(capability, set(case["capabilities"]) | {"native_ui"})
                    row = capabilities[capability]
                    required = set(row["requires"]) | set(row["conditional"].get(case["workload_kind"], []))
                    missing = sorted(required - set(case["available_inputs"]))
                    self.assertEqual(missing, expected)
                    state = "needs_input" if missing else "blocked"
                    reasons = ["needs_" + field for field in missing] if missing else ["provider_unqualified"]
                    readback = {"capability": capability, "state": state, "reason_codes": reasons,
                                "configuration_revision": 1, "binding_generation": 1,
                                "observed_at": fixture["fixed_clock"]}
                    self.assertTrue(valid("CapabilityReadiness", readback))
                    self.assertNotEqual(state, "ready")
                    if missing:
                        self.assertFalse(valid("CapabilityReadiness", readback | {"state": "ready"}))
                    self.assertFalse(valid("CapabilityReadiness", readback | {"reason_codes": ["invented_reason"]}))
        by_id = {case["id"]: case for case in fixture["cases"]}
        first = by_id["nonadp-first-project"]["expected_input_gaps"]
        second = by_id["nonadp-second-product-eks"]["expected_input_gaps"]
        sibling = by_id["sibling-project-without-telemetry"]["expected_input_gaps"]
        missing_namespace = by_id["eks-namespace-pending"]["expected_input_gaps"]
        self.assertEqual(first["investigation"], second["investigation"])
        self.assertNotEqual(first["release_readiness"], second["release_readiness"])
        self.assertEqual(sibling["release_readiness"], missing_namespace["release_readiness"])
        self.assertNotEqual(sibling["investigation"], missing_namespace["investigation"])
        connectors = {row["kind"]: row for row in matrix["connectors"]}
        for kind in fixture["unsupported_connectors"]:
            with self.subTest(connector=kind):
                self.assertEqual(connectors[kind]["status"], "unsupported")
                self.assertTrue(connectors[kind]["reason"])
        for kind in ("gitlab", "arbitrary_telemetry"):
            unsupported = portable_settings(by_id["nonadp-first-project"])
            unsupported["devops"]["integrations"] = [{"kind": kind, "connection_ref": record(),
                                                          "access_mode": "read", "event_support": False}]
            self.assertFalse(valid("ConfigurationInput", unsupported))
        invalid = portable_settings(by_id["nonadp-first-project"])
        invalid["devops"]["authentication_mode"] = "custom_oidc"
        self.assertFalse(valid("ConfigurationInput", invalid))

    def test_invalid_contracts(self):
        devops = configuration()
        security = configuration(service="security")
        for bad in (devops | {"capabilities": ["code_review"]},
                    devops | {"unknown": "secret"},
                    devops | {"devops": devops["devops"] | {"authentication_mode": "custom_oidc"}},
                    devops | {"devops": devops["devops"] | {"space_mode": "adopt"}},
                    security | {"capabilities": ["investigation"]},
                    security | {"devops": devops["devops"]}):
            with self.subTest(bad=bad):
                self.assertFalse(valid("ConfigurationInput", bad))
        run = {"configuration_revision": 1, "binding_id": "binding-example", "service_id": "devops",
               "capability": "release_readiness", "devops_input": {"kind": "release_readiness",
                   "repository_ref": record(kind="repository_commit"), "base_revision": "base-1",
                   "head_revision": "head-1"}}
        self.assertTrue(valid("RunInput", run))
        for bad in (run | {"capability": "prevention"},
                    run | {"devops_input": run["devops_input"] | {"head_revision": "latest"}},
                    run | {"devops_input": run["devops_input"] | {"kind": "investigation"}},
                    run | {"provider_role_arn": "untrusted"}):
            with self.subTest(bad=bad):
                self.assertFalse(valid("RunInput", bad))
        foreign = configuration()
        foreign["devops"]["accounts"][0]["environments"][0]["resource_refs"] = [record("project-other")]
        self.assertTrue(valid("ConfigurationInput", foreign))
        self.assertFalse(references_in_scope(foreign, "tenant-example", "project-example"))
        self.assertTrue(references_in_scope(devops, "tenant-example", "project-example"))
