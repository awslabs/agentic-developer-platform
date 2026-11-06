import unittest

from l5_services.profile_handoff import configuration_patch


class ProfileHandoffTests(unittest.TestCase):
    def setUp(self):
        self.reference = {
            "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "adp.profile",
            "record_type": "l5_configuration", "record_id": "config-a", "record_revision": "1",
        }
        self.profile = {
            "tenant_id": "tenant-a", "project_id": "project-a", "revision": 7,
            "configuration": {"basic": {"repositories": []}, "selected_capabilities": [
                {"capability": "threat_model", "ownership_mode": "managed"},
                {"capability": "Delivery", "ownership_mode": "integrated", "configuration_ref": {"record_id": "other"}},
            ]},
        }

    def test_preserves_other_profile_settings_and_replays_without_mutation(self):
        patch = configuration_patch(self.profile, self.reference, ["threat_model"], 7)
        self.assertEqual(patch["configuration"]["selected_capabilities"][0]["configuration_ref"], self.reference)
        self.assertEqual(patch["configuration"]["selected_capabilities"][1], self.profile["configuration"]["selected_capabilities"][1])
        self.assertNotIn("configuration_ref", self.profile["configuration"]["selected_capabilities"][0])
        self.assertEqual(patch["configuration"]["basic"], {"repositories": []})
        self.assertIsNone(configuration_patch({**self.profile, "configuration": patch["configuration"]}, self.reference, ["threat_model"], 7))

    def test_stale_foreign_and_unselected_handoffs_are_refused(self):
        for reference, capabilities, revision in (
            (self.reference, ["threat_model"], 6),
            ({**self.reference, "tenant_id": "foreign"}, ["threat_model"], 7),
            (self.reference, ["code_review"], 7),
            (self.reference, [], 7),
        ):
            with self.subTest(reference=reference, capabilities=capabilities, revision=revision), self.assertRaises(ValueError):
                configuration_patch(self.profile, reference, capabilities, revision)


class ProfileLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_replay_and_conditional_profile_revision(self):
        original = ProfileHandoffTests()
        original.setUp()
        profile = original.profile
        attempts = []

        async def read_profile():
            return profile

        async def patch_profile(patch, revision, key):
            attempts.append((patch, revision, key))
            profile["configuration"] = patch["configuration"]
            profile["revision"] += 1

        from l5_services.profile_handoff import link_configuration

        self.assertEqual(await link_configuration(read_profile, patch_profile, original.reference, ["threat_model"], "link-once"), "linked")
        self.assertEqual(attempts[0][1:], (7, "link-once"))
        self.assertEqual(await link_configuration(read_profile, patch_profile, original.reference, ["threat_model"], "link-once"), "linked")
        self.assertEqual(len(attempts), 1)

    async def test_conflict_and_outage_leave_link_retryable(self):
        import httpx
        from l5_services.profile_handoff import link_configuration

        original = ProfileHandoffTests()
        original.setUp()
        attempts = []

        async def read_profile():
            return original.profile

        async def patch_profile(patch, revision, key):
            attempts.append((revision, key))
            request = httpx.Request("PATCH", "https://product.example.test/configuration")
            raise httpx.HTTPStatusError("stale", request=request, response=httpx.Response(412, request=request))

        self.assertEqual(await link_configuration(read_profile, patch_profile, original.reference, ["threat_model"], "link-once"), "conflict")
        self.assertEqual(attempts, [(7, "link-once")])

        async def unavailable():
            raise httpx.ConnectError("unavailable")

        self.assertEqual(await link_configuration(unavailable, patch_profile, original.reference, ["threat_model"], "link-once"), "unavailable")
        self.assertEqual(attempts, [(7, "link-once")])
