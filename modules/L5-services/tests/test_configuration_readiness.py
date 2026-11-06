import unittest
from copy import deepcopy

from l5_services.configuration_readiness import assess_readiness, effective_monitoring
from l5_services.configuration_population import populate_settings


class ConfigurationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.document = {"record_type": "document", "record_id": "doc-a"}
        self.repository = {"record_type": "repository_commit", "record_id": "repo-a"}
        self.connection = {"record_type": "connection", "record_id": "connection-a"}
        self.settings = {
            "profile_ref": {"record_revision": "7"}, "purpose": "Review", "classification": "internal",
            "source_refs": [self.document, self.repository], "connection_ref": self.connection,
            "region": "us-east-1", "capabilities": ["threat_model", "code_review"],
            "enabled": False, "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 30,
                                         "concurrency": 1, "max_cost": "1.00", "currency": "USD", "policy_ref": {
                                             "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "policy-source",
                                             "record_type": "policy", "record_id": "budget-a", "record_revision": "1"}}, "triggers": [],
        }
        self.binding = {
            "binding_id": "binding-a", "configuration_id": "configuration-a", "state": "ready",
            "connection_ref": self.connection,
        }
        self.trigger = {
            "trigger_id": "trigger-a", "kind": "document_revision", "enabled": True,
            "capability": "threat_model", "binding_id": "binding-a", "source_refs": [self.document],
        }

    def assess(self, bindings=None, *, delegation_active=True):
        return assess_readiness(self.settings, "configuration-a", bindings or [], delegation_active=delegation_active)

    def test_disabled_draft_reports_missing_choices_without_binding_or_provider_calls(self):
        self.settings.pop("connection_ref")
        self.settings.pop("region")
        self.settings.pop("limits")
        state, gaps, monitoring = self.assess(delegation_active=False)
        self.assertEqual(state, "disabled")
        self.assertEqual(gaps, ["connection_ref", "region", "limits", "execution_delegation"])
        self.assertEqual(monitoring, {"poll_interval_seconds": 600, "poll_grace_seconds": 300})
        self.assertNotIn("stall_after_seconds", monitoring)
        self.assertNotIn("monitoring", self.settings)

    def test_ready_trigger_requires_owned_binding_and_compatible_source(self):
        self.settings["enabled"] = True
        self.settings["triggers"] = [self.trigger]
        state, gaps, _ = self.assess([self.binding])
        self.assertEqual((state, gaps), ("ready", []))
        for binding in ({**self.binding, "configuration_id": "configuration-other"},
                        {**self.binding, "connection_ref": {"record_id": "other"}}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                self.assess([binding])
        # Editing configuration must remain possible when a revision change
        # invalidates consent or a provider binding is not ready for execution.
        state, gaps, _ = self.assess([{**self.binding, "state": "suspended"}])
        self.assertEqual((state, gaps), ("needs_input", ["binding_not_ready"]))
        state, gaps, _ = self.assess([self.binding], delegation_active=False)
        self.assertEqual((state, gaps), ("needs_input", ["execution_delegation"]))
        self.trigger["source_refs"] = [self.repository]
        with self.assertRaises(ValueError):
            self.assess([self.binding])

    def test_enabled_targetless_trigger_and_schedule_are_rejected(self):
        self.settings["enabled"] = True
        self.settings["triggers"] = [{"kind": "manual", "enabled": True, "source_refs": []}]
        with self.assertRaises(ValueError):
            self.assess()
        self.settings["enabled"] = False
        self.settings["triggers"] = [{"kind": "manual", "enabled": False, "source_refs": []}]
        self.assertEqual(self.assess()[0], "disabled")
        self.settings["triggers"] = [{"kind": "schedule", "enabled": False}]
        with self.assertRaises(ValueError):
            self.assess()

    def test_incomplete_enabled_configuration_remains_needs_input(self):
        self.settings["enabled"] = True
        self.settings.pop("connection_ref")
        self.settings["source_refs"] = [self.document]
        state, gaps, _ = self.assess()
        self.assertEqual(state, "needs_input")
        self.assertEqual(gaps, ["connection_ref", "source_refs.code_review"])

    def test_well_architected_reports_missing_execution_inputs_only_when_enabled(self):
        settings = {**self.settings, "service_id": "well_architected", "capabilities": ["architecture_review"],
                    "source_refs": [self.document], "well_architected": {
                        "review_mode": "source_only", "pillars": ["SECURITY"],
                        "execution_role_ref": {"record_type": "role"}, "source_store_ref": {"record_type": "artifact_store"}}}
        settings.pop("classification")
        settings.pop("limits")
        disabled, gaps, _ = assess_readiness(settings, "configuration-a", [], delegation_active=True, service_id="well_architected")
        self.assertEqual(disabled, "disabled")
        self.assertEqual(gaps, ["catalog_mapping_unresolved", "provider_unqualified"])
        enabled, gaps, _ = assess_readiness({**settings, "enabled": True}, "configuration-a", [],
                                          delegation_active=True, service_id="well_architected")
        self.assertEqual(enabled, "needs_input")
        self.assertEqual(gaps, ["catalog_mapping_unresolved", "provider_unqualified", "classification", "limits"])
        _, gaps, _ = assess_readiness({**settings, "enabled": True, "classification": "internal", "limits": self.settings["limits"]},
                                     "configuration-a", [], delegation_active=True, service_id="well_architected")
        self.assertEqual(gaps, ["catalog_mapping_unresolved", "provider_unqualified"])
        self.assertNotIn("classification", settings)
        self.assertNotIn("limits", settings)

    def test_monitoring_defaults_custom_thresholds_and_deadline(self):
        self.settings["monitoring"] = {"poll_interval_seconds": 120, "stall_after_seconds": 600}
        self.assertEqual(effective_monitoring(self.settings), {
            "poll_interval_seconds": 120, "poll_grace_seconds": 300, "stall_after_seconds": 600,
        })
        for policy in ({"poll_interval_seconds": True}, {"poll_grace_seconds": 59},
                       {"poll_interval_seconds": 800, "stall_after_seconds": 1000},
                       {"stall_after_seconds": 4000}, {"stall_after_seconds": 86401},
                       {"poll_interval_seconds": 600, "unknown": 1}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                effective_monitoring({**self.settings, "monitoring": policy})


class DevopsReadinessTests(unittest.TestCase):
    def test_two_products_keep_scope_and_report_only_relevant_gaps(self):
        for project, workload in (("project-river", "aws_resource"), ("project-summit", "eks")):
            with self.subTest(project=project):
                def reference(kind):
                    return {"tenant_id": "tenant-example", "project_id": project,
                            "source_connection_id": "connection-a", "record_type": kind,
                            "record_id": f"{kind}-{project}", "record_revision": "7"}

                profile = {"tenant_id": "tenant-example", "project_id": project,
                           "product_id": f"product-{project}", "revision": 7,
                           "configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "owned"}],
                                             "advanced": {"environments": [reference("environment")]}}}
                profile_ref = reference("product") | {"record_id": profile["product_id"]}
                available = {"coverage": "complete", "next_cursor": None, "items": [{
                    "connection_ref": {"id": "connection-a"}, "record_ref": reference("connection"),
                    "resources": [reference("environment")]}]}
                environment = {"environment_ref": reference("environment"), "workload_kind": workload,
                               "resource_refs": [reference("resource")]}
                if workload == "eks":
                    environment["cluster_ref"] = reference("cluster")
                choices = {"service_id": "devops", "capabilities": ["investigation", "release_readiness"],
                           "enabled": False, "triggers": [], "devops": {
                               "space_mode": "adopt", "existing_space_id": f"space-{project}",
                               "accounts": [{"environments": [environment]}],
                               "integrations": [{"kind": "cloudwatch", "resource_refs": [reference("telemetry")]}]}}
                settings, inherited = populate_settings(profile, profile_ref, available, choices, "devops")
                choices["devops"]["accounts"].clear()
                self.assertEqual(settings["profile_ref"], profile_ref)
                self.assertEqual(settings["source_refs"], [reference("environment")])
                self.assertEqual(settings["devops"]["accounts"][0]["environments"][0]["workload_kind"], workload)
                self.assertIn("source_refs", inherited)
                state, gaps, _ = assess_readiness(settings, "config-a", [], delegation_active=False,
                                                  service_id="devops")
                self.assertEqual(state, "disabled")
                self.assertIn("release_readiness.needs_repository", gaps)
                self.assertNotIn("investigation.needs_telemetry", gaps)
                self.assertEqual("investigation.needs_namespace" in gaps, workload == "eks")
                self.assertFalse(any("release_readiness.needs_namespace" in gap for gap in gaps))

    def test_unverified_inputs_never_report_ready_or_accept_automation(self):
        settings = {"enabled": False, "triggers": [], "capabilities": ["investigation"],
                    "devops": {"space_mode": "adopt", "existing_space_id": "space-a",
                               "accounts": [{"environments": [{"workload_kind": "aws_resource",
                                                                "resource_refs": [{"record_id": "resource-a"}]}]}],
                               "integrations": [{"kind": "cloudwatch", "resource_refs": [{"record_id": "telemetry-a"}]}]}}

        def assess(value):
            return assess_readiness(value, "config-a", [], delegation_active=True, service_id="devops")

        self.assertEqual(assess(settings)[:2], ("disabled", ["investigation.provider_unqualified"]))
        for change in ({"capabilities": ["investigation", "investigation"]},
                       {"enabled": True}, {"triggers": [{"kind": "schedule", "enabled": True}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                assess({**settings, **change})
        candidate = deepcopy(settings)
        candidate["devops"]["accounts"] = []
        self.assertIn("investigation.needs_account", assess(candidate)[1])

    def test_multiple_authorized_environments_need_an_explicit_selection(self):
        def reference(kind, identifier):
            return {"tenant_id": "tenant-example", "project_id": "project-river",
                    "source_connection_id": "connection-a", "record_type": kind,
                    "record_id": identifier, "record_revision": "7"}

        first = reference("environment", "environment-one")
        second = reference("environment", "environment-two")
        profile = {"tenant_id": "tenant-example", "project_id": "project-river", "product_id": "product-river",
                   "revision": 7, "configuration": {"selected_capabilities": [
                       {"capability": "devops", "ownership_mode": "owned"}],
                       "advanced": {"environments": [first, second]}}}
        profile_ref = reference("product", "product-river")
        available = {"coverage": "complete", "next_cursor": None, "items": [{
            "connection_ref": {"id": "connection-a"}, "resources": [first, second]}]}
        choices = {"service_id": "devops", "enabled": False, "triggers": [],
                   "capabilities": ["investigation"], "devops": {"accounts": [], "integrations": []}}
        with self.assertRaisesRegex(ValueError, "explicit selection"):
            populate_settings(profile, profile_ref, available, choices, "devops")
        choices["source_refs"] = [first]
        choices["overrides"] = [{"field": "source_refs", "value": [first], "reason": "Use river's API environment"}]
        settings, inherited = populate_settings(profile, profile_ref, available, choices, "devops")
        self.assertEqual(settings["source_refs"], [first])
        self.assertNotIn("source_refs", inherited)
        self.assertIn("investigation.needs_account", assess_readiness(
            settings, "config-river", [], delegation_active=False, service_id="devops")[1])
        choices["source_refs"] = [first, first]
        choices["overrides"][0]["value"] = [first, first]
        with self.assertRaisesRegex(ValueError, "duplicate source_ref"):
            populate_settings(profile, profile_ref, available, choices, "devops")

    def test_project_bound_sources_connections_and_policies_have_no_platform_default(self):
        for product_id, project_id, region in (
            ("product-river", "project-river-api", "example-region-1"),
            ("product-river", "project-river-worker", "example-region-2"),
            ("product-summit", "project-summit-api", "example-region-3"),
        ):
            with self.subTest(project=project_id):
                connection_id = "connection-" + project_id

                def reference(kind, identifier):
                    return {"tenant_id": "tenant-example", "project_id": project_id,
                            "source_connection_id": connection_id, "record_type": kind,
                            "record_id": identifier, "record_revision": "7"}

                environment = reference("environment", "environment-" + project_id)
                connection = reference("connection", connection_id)
                policy = reference("policy", "policy-" + project_id)
                profile = {"tenant_id": "tenant-example", "product_id": product_id, "project_id": project_id,
                           "revision": 7, "configuration": {"selected_capabilities": [
                               {"capability": "devops", "ownership_mode": "owned"}],
                               "advanced": {"environments": [environment]}}}
                available = {"coverage": "complete", "next_cursor": None, "items": [{
                    "connection_ref": {"id": connection_id}, "record_ref": connection,
                    "resources": [environment]}]}
                choices = {"service_id": "devops", "enabled": False, "triggers": [],
                           "capabilities": ["investigation", "release_readiness"], "devops": {
                               "hosting_connection_ref": connection, "region": region, "space_mode": "create",
                               "accounts": [{"connection_ref": connection, "environments": [{
                                   "environment_ref": environment, "workload_kind": "aws_resource",
                                   "resource_refs": [reference("resource", "resource-" + project_id)]}]}],
                               "access_policy_ref": policy, "integrations": []}}
                profile_ref = reference("product", product_id)
                settings, _ = populate_settings(profile, profile_ref, available, choices, "devops")
                self.assertEqual(settings["source_refs"], [environment])
                self.assertEqual(settings["devops"]["hosting_connection_ref"], connection)
                self.assertEqual(settings["devops"]["access_policy_ref"], policy)
                self.assertEqual(settings["devops"]["region"], region)
                self.assertNotIn("connection_ref", settings)
                gaps = assess_readiness(settings, "config-a", [], delegation_active=False, service_id="devops")[1]
                self.assertIn("investigation.needs_telemetry", gaps)
                self.assertIn("release_readiness.needs_repository", gaps)
                self.assertNotIn("release_readiness.needs_telemetry", gaps)
                for field, invalid in (("access_policy_ref", policy | {"project_id": "foreign-project"}),
                                       ("hosting_connection_ref", connection | {"record_id": "other-connection"})):
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        populate_settings(profile, profile_ref, available, {
                            **choices, "devops": {**choices["devops"], field: invalid}}, "devops")
                with self.assertRaises(ValueError):
                    populate_settings(profile, profile_ref, available, {
                        **choices, "devops": {**choices["devops"], "accounts": [{
                            "environments": [{"environment_ref": environment | {"project_id": "foreign-project"}}]}]}},
                        "devops")
                choices["devops"].pop("hosting_connection_ref")
                choices["devops"].pop("region")
                settings, _ = populate_settings(profile, profile_ref, available, choices, "devops")
                self.assertNotIn("hosting_connection_ref", settings["devops"])
                self.assertNotIn("region", settings["devops"])
