"""Offline S01 candidate map invariants; not native-source acceptance."""

import copy
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def validate_map(candidate):
    providers = candidate["providers"]
    if len(providers) != 2 or {item["provider"] for item in providers} != {"github", "jira"}:
        raise ValueError("two distinct providers, including a new Profile provider, required")
    if "not implemented" not in providers[1]["current_profile_capability"]:
        raise ValueError("new provider must not be described as already supported")
    for provider in providers:
        if provider["registered_connection_ref"] is not None or provider["native_fixture_ref"] is not None:
            raise ValueError("unverified registry identity or native fixture claimed")
        if provider["native_auth_verification"] != "pending_source_owner":
            raise ValueError("unverified auth path claimed")
        if not provider["fields"] or any(not field["source"].startswith(f"backend/plugins/{provider['provider']}/models/") for field in provider["fields"]):
            raise ValueError("missing or mismatched native model")
    if candidate["native_fixture_inventory"] != {"github": "pending_source_owner", "jira": "pending_source_owner"}:
        raise ValueError("invented native fixture inventory")
    if not any(concept["concept"] == "issue" and set(concept["providers"]) == {"github", "jira"} for concept in candidate["shared_concepts"]):
        raise ValueError("no two-source normalization concept")


class SourceFieldMapTests(unittest.TestCase):
    def setUp(self):
        self.mapping = json.loads((ROOT / "source-field-map.json").read_text())

    def test_two_tool_mapping_matches_pinned_offline_source_audit(self):
        validate_map(self.mapping)
        audit = (ROOT / "audit.md").read_text()
        gateway = (ROOT.parents[2] / "modules/gateway/src/product_onboarding/source_discovery.py").resolve().read_text()
        scopes = (ROOT.parents[2] / "modules/gateway/src/shared/identity/oauth_providers.py").resolve().read_text()
        self.assertIn("listGithubRemoteScopes", (ROOT / "architecture.md").read_text())
        self.assertIn("queryJiraAgileBoards", (ROOT / "architecture.md").read_text())
        self.assertIn("backend/plugins/jira/api/remote_api.go", audit)
        assert "provider not in (\"gitlab\", \"github\", \"aws\")" in gateway
        assert '"atlassian": ("jira", ["read:jira-work", "read:me", "offline_access"])' in scopes
        self.assertTrue(all(field["type"] and field["rule"] for provider in self.mapping["providers"] for field in provider["fields"]))

    def test_reject_one_source_and_false_verification(self):
        for mutation in (lambda m: m["providers"].pop(),
                         lambda m: m["providers"][1].update(registered_connection_ref="invented"),
                         lambda m: m["native_fixture_inventory"].update(jira="invented"),
                         lambda m: m["providers"][1].update(native_auth_verification="verified")):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                candidate = copy.deepcopy(self.mapping)
                mutation(candidate)
                validate_map(candidate)


class NativeReviewProcedureTests(unittest.TestCase):
    def test_scn02_oracles_bounds_and_pending_owner_evidence(self):
        verification = (ROOT / "verification.md").read_text()
        procedure = verification.split("## S01 SCN-02: two-provider native comparison", 1)[1].split("## Fixture and determinism contract", 1)[0]
        for requirement in ("source-owner access and native fixtures not verified", "*native* APIs", "GitHub", "Jira", "actual registered connection", "current ADP binding", "ProjectId", "equal issue titles", "colliding native IDs", "revoked grant", "shared/private", "Profile save", "100 records/page", "30-second", "bounded poll deadline", "revoke temporary fixture grants", "private evidence", "AC-02/SCN-02"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, procedure)
        self.assertTrue((ROOT / "source-field-map.json").exists())
        self.assertIn("Independent source-owner verification and acceptance-owner decision remain outstanding", procedure)


if __name__ == "__main__":
    unittest.main()
