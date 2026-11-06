import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake"


class OptionalModeTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(MODULE))
        self.addCleanup(lambda: sys.path.remove(str(MODULE)))
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        import deploy
        import deploy_connectors
        import mode

        self.collection = deploy
        self.connectors = deploy_connectors
        self.mode = mode
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.path = Path(self.private.name) / "operator.json"
        self.collection_config = dict.fromkeys(deploy.REQUIRED, "fixture")
        self.collection_config.update(enabled=True, connectors_enabled=True, environment="dev",
                                      aws_account_id="000000000000", aws_region="us-east-1",
                                      deployment_role="fixture-role", migration_username="migrator",
                                      writer_db_username="writer", reader_db_username="reader")
        self.connector_config = {
            "enabled": True, "environment": "dev", "aws_account_id": "000000000000",
            "aws_region": "us-east-1", "deployment_label": "fixture", "deployment_role": "fixture-role",
            "api_receipt": "/private/api.json",
            "api_role_boundary_arn": "arn:aws:iam::000000000000:policy/fixture-api",
            "provider_egress_cidrs": ["198.51.100.0/24"],
        }

    def select(self, installer, config):
        self.path.write_text(json.dumps(config))
        self.path.chmod(0o600)
        return installer.selected_config(self.path)

    def test_absent_and_false_defaults_create_no_selection(self):
        self.assertIsNone(self.collection.selected_config(None))
        self.assertIsNone(self.connectors.selected_config(None))
        self.assertIsNone(self.select(self.collection, {"enabled": False}))
        self.assertIsNone(self.select(self.connectors, {"enabled": False}))
        self.assertEqual(self.mode.render(False, False), {
            self.mode.MASTER: "false", self.mode.COLLECTION: "false"})

    def test_connector_only_and_full_use_case_insensitive_canonical_flags(self):
        with mock.patch.dict(os.environ, {self.mode.MASTER: "TrUe", self.mode.COLLECTION: "FaLsE"}):
            self.assertEqual(self.select(self.connectors, self.connector_config), self.connector_config)
            self.assertIsNone(self.select(self.collection, {"enabled": False, "connectors_enabled": True}))
            self.assertEqual(self.mode.render(True, False), {
                self.mode.MASTER: "true", self.mode.COLLECTION: "false"})
        with mock.patch.dict(os.environ, {self.mode.MASTER: "TRUE", self.mode.COLLECTION: "true"}):
            self.assertEqual(self.select(self.connectors, self.connector_config), self.connector_config)
            self.assertEqual(self.select(self.collection, self.collection_config), self.collection_config)
            self.assertEqual(self.mode.render(True, True), {
                self.mode.MASTER: "true", self.mode.COLLECTION: "true"})

    def test_invalid_or_collection_only_flags_fail_before_credentials(self):
        cases = (
            (self.collection, {"enabled": True, "connectors_enabled": False}, {}, "collection requires"),
            (self.collection, {"enabled": False, "connectors_enabled": "false"}, {}, "explicit booleans"),
            (self.collection, {"enabled": "false"}, {}, "explicit booleans"),
            (self.collection, {"enabled": None, "connectors_enabled": True}, {}, "explicit booleans"),
            (self.collection, {"enabled": None, "connectors_enabled": True},
             {self.mode.MASTER: "true", self.mode.COLLECTION: "true"}, "explicit booleans"),
            (self.collection, {"connectors_enabled": True}, {}, "explicit booleans"),
            (self.connectors, {"enabled": "true"}, {}, "explicit booleans"),
            (self.collection, {"enabled": False, "unknown_flag": "true"}, {}, "unknown optional"),
            (self.connectors, {"enabled": False, "unknown_flag": "true"}, {}, "unknown optional"),
            (self.connectors, {"enabled": False}, {self.mode.MASTER: "yes"}, "invalid optional"),
            (self.collection, {"enabled": False}, {self.mode.COLLECTION: "true"}, "collection requires"),
            (self.connectors, self.connector_config, {self.mode.MASTER: "false"}, "differs"),
            (self.collection, self.collection_config, {self.mode.MASTER: "true",
                                                      self.mode.COLLECTION: "false"}, "differs"),
        )
        for installer, config, overrides, reason in cases:
            with self.subTest(config=config, overrides=overrides), mock.patch.dict(os.environ, overrides), \
                    mock.patch.object(installer, "checked_install") as credentials, \
                    mock.patch.object(installer, "terraform_run", create=True) as terraform, \
                    mock.patch.object(sys, "argv", ["deploy", "--config", str(self.path),
                                                      "--plan-file", str(self.path.parent / "plan")]), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.path.write_text(json.dumps(config))
                self.path.chmod(0o600)
                with self.assertRaises(SystemExit) as exit_code:
                    installer.main()
                self.assertEqual(exit_code.exception.code, 2)
                self.assertIn(reason, errors.getvalue())
                credentials.assert_not_called()
                terraform.assert_not_called()

    def test_invalid_environment_flags_reject_without_private_config(self):
        for name in (self.mode.MASTER, self.mode.COLLECTION):
            for value in ("1", "yes", " true ", "0"):
                with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}):
                    for installer in (self.connectors, self.collection):
                        with self.assertRaisesRegex(ValueError, "invalid optional deployment flag"):
                            installer.selected_config(None)


if __name__ == "__main__":
    unittest.main()
