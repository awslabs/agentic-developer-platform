from copy import deepcopy
import unittest

from l5_services.configuration_population import configuration_options, populate_settings


class ConfigurationPopulationTests(unittest.TestCase):
    def setUp(self):
        self.profile_ref = self.reference("product", "product-a", "7", "adp.profile")
        self.repository = self.reference("repository_commit", "repo-a", "sha-a")
        self.document = self.reference("document", "doc-a", "revision-a", "connection-b")
        self.profile = {
            "tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a", "revision": 7,
            "product": {"purpose": "Review the design"},
            "configuration": {
                "basic": {"repositories": [self.repository], "documents": [self.document]},
                "governance": {"data_classification": "internal"},
            },
        }
        self.sources = {
            "coverage": "complete", "next_cursor": None,
            "items": [
                {"connection_ref": {"kind": "credential", "id": "connection-a"}, "resources": [self.repository]},
                {"connection_ref": {"kind": "credential", "id": "connection-b"},
                 "record_ref": self.reference("connection", "connection-b", "2", "connection-b"),
                 "resources": [self.document]},
            ],
        }

    def reference(self, kind, identifier, revision, connection="connection-a"):
        return {
            "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": connection,
            "record_type": kind, "record_id": identifier, "record_revision": revision,
        }

    def test_inherits_pinned_values_without_guessing_connection(self):
        settings, inherited = populate_settings(self.profile, self.profile_ref, self.sources, {})
        self.assertEqual(settings["profile_ref"], self.profile_ref)
        self.assertEqual(settings["source_refs"], [self.repository, self.document])
        self.assertEqual(settings["purpose"], "Review the design")
        self.assertEqual(settings["classification"], "internal")
        self.assertEqual(inherited, ["purpose", "classification", "source_refs"])
        self.assertNotIn("connection_ref", settings)
        settings["source_refs"].clear()
        self.assertEqual(self.profile["configuration"]["basic"]["repositories"], [self.repository])

    def test_security_uses_commit_type_for_profile_options_consent_and_triggers(self):
        repository = self.reference("repository", "repo-a", "a" * 40)
        commit = {**repository, "record_type": "repository_commit"}
        self.profile["configuration"]["basic"]["repositories"] = [repository]
        self.sources["items"][0]["resources"] = [repository]
        options = configuration_options(self.profile, self.sources, "security")
        self.assertIn(commit, options["sources"])
        choices = {
            "source_refs": [repository],
            "overrides": [{"field": "source_refs", "value": [repository], "reason": "Use the selected repository"}],
            "triggers": [{"kind": "manual", "source_refs": [repository]}],
        }
        settings, _ = populate_settings(self.profile, self.profile_ref, self.sources, choices, "security")
        self.assertEqual(settings["source_refs"], [commit])
        self.assertEqual(settings["overrides"][0]["value"], [commit])
        self.assertEqual(settings["triggers"][0]["source_refs"], [commit])
        inherited, _ = populate_settings(self.profile, self.profile_ref, self.sources, {}, "security")
        self.assertIn(commit, inherited["source_refs"])
        self.assertEqual(choices["source_refs"], [repository])
        self.assertEqual(self.profile["configuration"]["basic"]["repositories"], [repository])

    def test_explicit_selections_override_only_the_chosen_fields(self):
        connection = self.reference("connection", "connection-b", "2", "connection-b")
        settings, inherited = populate_settings(self.profile, self.profile_ref, self.sources, {
            "source_refs": [self.document], "connection_ref": connection, "classification": "confidential",
            "exclusions": ["old-path/"],
            "overrides": [
                {"field": "source_refs", "value": [self.document], "reason": "Exclude the old repository"},
                {"field": "connection_ref", "value": connection, "reason": "Use document connection"},
                {"field": "classification", "value": "confidential", "reason": "Review privately"},
            ],
        })
        self.assertEqual(settings["source_refs"], [self.document])
        self.assertEqual(settings["connection_ref"], connection)
        self.assertEqual(settings["classification"], "confidential")
        self.assertEqual(settings["exclusions"], ["old-path/"])
        self.assertEqual(len(settings["overrides"]), 3)
        self.assertEqual(inherited, ["purpose"])

    def test_foreign_and_unauthorized_references_are_not_inherited_or_selectable(self):
        foreign = {**self.repository, "tenant_id": "tenant-b"}
        self.profile["configuration"]["basic"]["repositories"].append(foreign)
        self.sources["items"][0]["resources"] = []
        settings, _ = populate_settings(self.profile, self.profile_ref, self.sources, {})
        self.assertEqual(settings["source_refs"], [self.document])
        for choices in ({"source_refs": [foreign]}, {"source_refs": [self.repository]},
                        {"connection_ref": self.reference("connection", "unlisted", "1")}):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                populate_settings(self.profile, self.profile_ref, self.sources, choices)

    def test_profile_revision_and_source_coverage_must_be_current(self):
        with self.assertRaises(ValueError):
            populate_settings(self.profile, {**self.profile_ref, "record_revision": "6"}, self.sources, {})
        with self.assertRaises(ValueError):
            populate_settings(self.profile, self.profile_ref, {**self.sources, "coverage": "partial"}, {})
        with self.assertRaises(ValueError):
            populate_settings(self.profile, self.profile_ref, self.sources, {"provider_credentials": "not allowed"})
        with self.assertRaises(ValueError):
            populate_settings(self.profile, self.profile_ref, self.sources, {"classification": "restricted"})
        with self.assertRaises(ValueError):
            populate_settings(self.profile, self.profile_ref, self.sources, {"overrides": [
                {"field": "classification", "reason": "fake", "value": "restricted"}]})

    def test_options_do_not_invent_connections_or_expose_incomplete_sources(self):
        options = configuration_options(self.profile, self.sources)
        self.assertEqual(options["connections"], [self.reference("connection", "connection-b", "2", "connection-b")])
        self.assertEqual(options["sources"], [self.repository, self.document])
        self.assertIn("region_options_unavailable", options["unavailable_reasons"])
        incomplete = configuration_options(self.profile, {**self.sources, "next_cursor": "next"})
        self.assertEqual(incomplete["connections"], [])
        self.assertEqual(incomplete["sources"], [])
        self.assertIn("source_coverage_incomplete", incomplete["unavailable_reasons"])
        missing = configuration_options(self.profile, {**self.sources, "items": [self.sources["items"][0]]})
        self.assertEqual(missing["connections"], [])
        self.assertIn("connection_revision_unavailable", missing["unavailable_reasons"])

    def test_connection_revision_and_scope_must_match_authorized_identity(self):
        connection = self.reference("connection", "connection-b", "2", "connection-b")
        for invalid in (
            {**connection, "record_revision": "3"},
            {**connection, "tenant_id": "foreign"},
            {**connection, "record_type": "credential"},
            {**connection, "source_connection_id": "connection-a"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                populate_settings(self.profile, self.profile_ref, self.sources, {
                    "connection_ref": invalid,
                    "overrides": [{"field": "connection_ref", "value": invalid, "reason": "Explicit choice"}],
                })
        invalid_sources = {**self.sources, "items": [
            self.sources["items"][0], {**self.sources["items"][1], "record_ref": {**connection, "record_revision": "latest"}},
        ]}
        self.assertEqual(configuration_options(self.profile, invalid_sources)["connections"], [])

    def test_well_architected_options_expose_only_the_selected_capability(self):
        for selection in (
            {"capability": "well_architected"},
            {"capability": "well_architected", "ownership_mode": "managed"},
        ):
            with self.subTest(selection=selection):
                self.profile["configuration"]["selected_capabilities"] = [selection]
                options = configuration_options(self.profile, self.sources, "well_architected")
                self.assertEqual(options["capabilities"], ["architecture_review"])
                self.assertEqual(options["sources"], [self.repository, self.document])
                self.assertNotIn("catalog_mapping_unresolved", options["unavailable_reasons"])
                self.assertIn("provider_unqualified", options["unavailable_reasons"])
                self.assertEqual(options["regions"], [])

    def test_well_architected_options_do_not_select_foreign_or_disabled_capabilities(self):
        for selections in (
            [], [{"capability": "finops"}], [{"capability": "architecture_review"}],
            [{"capability": "well_architected", "ownership_mode": "not_applicable"}],
            [None, "well_architected"],
        ):
            with self.subTest(selections=selections):
                self.profile["configuration"]["selected_capabilities"] = selections
                options = configuration_options(self.profile, self.sources, "well_architected")
                self.assertEqual(options["capabilities"], [])
                self.assertIn("capability_not_selected", options["unavailable_reasons"])

    def test_well_architected_selection_does_not_bypass_source_coverage(self):
        self.profile["configuration"]["selected_capabilities"] = [{"capability": "well_architected"}]
        options = configuration_options(self.profile, {**self.sources, "next_cursor": "next"}, "well_architected")
        self.assertEqual(options["capabilities"], ["architecture_review"])
        self.assertEqual(options["sources"], [])
        self.assertEqual(options["connections"], [])
        self.assertIn("source_coverage_incomplete", options["unavailable_reasons"])

    def test_review_services_project_exact_repository_pin_without_changing_product(self):
        repository = {**self.repository, "record_type": "repository", "record_revision": "a" * 40}
        self.profile["configuration"]["basic"]["repositories"] = [repository]
        self.sources["items"][0]["resources"] = [repository]
        expected = [{**repository, "record_type": "repository_commit"}, self.document]
        profile_before, sources_before = deepcopy(self.profile), deepcopy(self.sources)
        choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
                   "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]}}
        options = configuration_options(self.profile, self.sources, "well_architected")
        settings, inherited = populate_settings(self.profile, self.profile_ref, self.sources, choices, "well_architected")
        self.assertEqual(options["sources"], expected)
        self.assertEqual(settings["source_refs"], expected)
        self.assertIn("source_refs", inherited)
        raw_sources = [repository, self.document]
        explicit = {**choices, "source_refs": raw_sources,
                    "overrides": [{"field": "source_refs", "value": raw_sources, "reason": "Keep selected source pins"}],
                    "triggers": [{"kind": "manual", "enabled": False, "source_refs": raw_sources}]}
        original = deepcopy(explicit)
        settings, _ = populate_settings(self.profile, self.profile_ref, self.sources, explicit, "well_architected")
        self.assertEqual(settings["source_refs"], expected)
        self.assertEqual(settings["overrides"][0]["value"], expected)
        self.assertEqual(settings["triggers"][0]["source_refs"], expected)
        self.assertEqual(explicit, original)
        self.assertEqual(self.profile, profile_before)
        self.assertEqual(self.sources, sources_before)
        self.assertEqual(configuration_options(self.profile, self.sources)["sources"], expected)
        self.assertEqual(populate_settings(self.profile, self.profile_ref, self.sources, {})[0]["source_refs"], expected)
        for altered in ({**expected[0], "record_revision": "b" * 40}, {**expected[0], "tenant_id": "foreign"}):
            invalid = {**choices, "source_refs": [altered],
                       "overrides": [{"field": "source_refs", "value": [altered], "reason": "Changed choice"}]}
            with self.subTest(altered=altered), self.assertRaisesRegex(ValueError, "currently authorized"):
                populate_settings(self.profile, self.profile_ref, self.sources, invalid, "well_architected")

    def test_well_architected_keeps_unpinned_repository_visible_with_gap(self):
        from l5_services.configuration_readiness import assess_readiness

        repository = {**self.repository, "record_type": "repository", "record_revision": "main"}
        self.profile["configuration"]["basic"]["repositories"] = [repository]
        self.sources["items"][0]["resources"] = [repository]
        choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
                   "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]}}
        options = configuration_options(self.profile, self.sources, "well_architected")
        settings, _ = populate_settings(self.profile, self.profile_ref, self.sources, choices, "well_architected")
        self.assertEqual(options["sources"], [repository, self.document])
        self.assertEqual(settings["source_refs"], [repository, self.document])
        self.assertIn("repository_revision_unpinned", options["unavailable_reasons"])
        for enabled in (False, True):
            state, gaps, _ = assess_readiness({**settings, "enabled": enabled}, "config", [],
                                            delegation_active=False, service_id="well_architected")
            self.assertEqual(state, "needs_input" if enabled else "disabled")
            self.assertIn("repository_revision_unpinned", gaps)

    def test_finops_and_well_architected_choices_stay_disjoint(self):
        self.profile["configuration"]["selected_capabilities"] = [{"capability": "finops"}]
        cases = (
            ("finops", "well_architected", {"service_id": "finops", "finops": {}}),
            ("well_architected", "finops", {
                "service_id": "well_architected", "capabilities": ["architecture_review"],
                "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
            }),
        )
        for service_id, foreign_service, choices in cases:
            with self.subTest(service_id=service_id):
                settings, _ = populate_settings(
                    self.profile, self.profile_ref, self.sources, choices, service_id,
                )
                self.assertEqual(settings["service_id"], service_id)
                self.assertEqual(settings[service_id], choices[service_id])
                self.assertNotIn(foreign_service, settings)
                with self.assertRaises(ValueError):
                    populate_settings(self.profile, self.profile_ref, self.sources,
                                      {**choices, foreign_service: {}}, service_id)

    def test_finops_rejects_transform_choices(self):
        self.profile["configuration"]["selected_capabilities"] = [{"capability": "finops"}]
        with self.assertRaises(ValueError):
            populate_settings(self.profile, self.profile_ref, self.sources, {
                "service_id": "finops", "finops": {}, "transform": {},
            }, "finops")

    def test_finops_preserves_explicit_scope_without_inheriting_repository(self):
        self.profile["configuration"]["selected_capabilities"] = [{"capability": "finops"}]
        connection = self.reference("connection", "connection-b", "2", "connection-b")
        selections = {
            "service_id": "finops", "connection_ref": connection, "region": "us-east-1",
            "overrides": [
                {"field": "connection_ref", "value": connection, "reason": "Chosen billing connection"},
                {"field": "region", "value": "us-east-1", "reason": "Chosen billing region"},
            ],
            "finops": {"agent_mode": "adopt", "native_agent_ref": "agent-example",
                       "data_role_ref": "role-example",
                       "billing_scope": {"account_ids": ["123456789012"], "view_ref": "view-example"}},
        }
        settings, inherited = populate_settings(
            self.profile, self.profile_ref, self.sources, selections, "finops",
        )
        self.assertEqual(settings["profile_ref"], self.profile_ref)
        self.assertEqual(settings["source_refs"], [])
        self.assertEqual(settings["finops"], selections["finops"])
        self.assertNotIn("finops", inherited)
        selections["finops"]["billing_scope"]["account_ids"].append("999999999999")
        self.assertEqual(settings["finops"]["billing_scope"]["account_ids"], ["123456789012"])
        for invalid in (
            {"billing_scope": {"account_ids": ["123456789012"], "view_ref": ""}},
            {"billing_scope": {"account_ids": ["123456789012", "123456789012"], "view_ref": "view-example"}},
            {"agent_mode": "adopt", "native_agent_ref": ""},
            {"provider_credentials": "not allowed"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                populate_settings(self.profile, self.profile_ref, self.sources,
                                  {**selections, "finops": invalid}, "finops")
        with self.assertRaises(ValueError):
            populate_settings(self.profile, {**self.profile_ref, "record_revision": "HEAD"},
                              self.sources, selections, "finops")
