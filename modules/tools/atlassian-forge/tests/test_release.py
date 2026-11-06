"""Publication must resolve the real environment before preparing ADP setup."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("forge_release", Path(__file__).resolve().parents[1] / "release.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
APP = "ari:cloud:ecosystem::app/11111111-1111-1111-1111-111111111111"
ENV = "22222222-2222-2222-2222-222222222222"
ROWS = [{"id": ENV, "name": "production", "type": "PRODUCTION", "lastDeployedAt": "2026-01-01T00:00:00.000Z"}]
MANIFEST = ("modules:\n  jira:adminPage: []\napp:\n  runtime:\n    name: nodejs22.x\n  id: " + APP + "\n").encode()


class ReleaseTests(unittest.TestCase):
    def test_adp_dev_still_publishes_to_shareable_forge_production(self):
        values = {"FORGE_APP_ID": APP, "ADP_BASE_URL": "https://dev.example.com/", "FORGE_API_TOKEN": "must-not-copy"}
        settings = module.release_settings(values)
        result = module.receipt(settings, ROWS, "a" * 40, MANIFEST)
        self.assertEqual(result["environment_name"], "production")
        self.assertEqual(result["environment_id"], ENV)
        self.assertNotIn("must-not-copy", str(result))
        self.assertNotIn("gateway_configuration", result)
        self.assertFalse(result["pairing_verified"])
        self.assertFalse(result["automatic_refresh_verified"])
        values["FORGE_ENVIRONMENT"] = "development"
        with self.assertRaisesRegex(ValueError, "installation links"):
            module.release_settings(values)

    def test_unknown_duplicate_wrong_type_and_malformed_environment_are_refused(self):
        for rows in ({"environments": ROWS}, [], ROWS * 2, [{**ROWS[0], "type": "DEVELOPMENT"}], [{**ROWS[0], "id": "not-an-id"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                module.production_environment(rows)

    def test_only_a_vendor_install_link_can_prepare_activation(self):
        url = "https://developer.atlassian.com/console/install/example-link"
        self.assertEqual(module.activation_inputs(APP, ENV, url), {"app_id": APP, "environment_id": ENV, "install_url": url})
        for invalid in ("https://evil.example/console/install/x", url + "?token=secret", "https://secret@developer.atlassian.com/console/install/x"):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                module.activation_inputs(APP, ENV, invalid)

    def test_publication_refuses_unknown_identity_or_credential_bearing_origin(self):
        for change in ({"FORGE_APP_ID": "unregistered"}, {"ADP_BASE_URL": "https://secret@dev.example.com"}, {"ADP_BASE_URL": "http://dev.example.com"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.release_settings({"FORGE_APP_ID": APP, "ADP_BASE_URL": "https://dev.example.com", **change})

    def test_registration_needs_a_selected_space_and_cannot_replace_an_app(self):
        values = {"ADP_BASE_URL": "https://dev.example.com", "FORGE_APP_NAME": "ADP Jira", "FORGE_DEVELOPER_SPACE_ID": ENV}
        self.assertEqual(module.release_settings(values, for_registration=True)["app_id"], "")
        for change in ({"FORGE_APP_ID": APP}, {"FORGE_DEVELOPER_SPACE_ID": ""}, {"FORGE_APP_NAME": "--help"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.release_settings({**values, **change}, for_registration=True)

    def test_app_root_matches_the_release_and_module_ids_cannot_replace_it(self):
        self.assertEqual(module.registered_app_id(MANIFEST.decode()), APP)
        self.assertEqual(module.registered_app_id('app:\n  id: "' + APP + '"\n'), APP)
        with self.assertRaises(ValueError):
            module.registered_app_id('modules:\n  id: ' + APP + '\napp:\n  runtime:\n    name: nodejs22.x\n')
        with self.assertRaises(ValueError):
            module.receipt(module.release_settings({"FORGE_APP_ID": APP, "ADP_BASE_URL": "https://dev.example.com"}), ROWS, "a" * 40, MANIFEST.replace(APP.encode(), ('ari:cloud:ecosystem::app/' + ENV).encode()))


if __name__ == "__main__":
    unittest.main()
