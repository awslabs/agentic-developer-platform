import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake"


class OptionalIntentTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(MODULE))
        self.addCleanup(lambda: sys.path.remove(str(MODULE)))
        import intent_ledger

        self.ledger = intent_ledger
        self.config = {"environment": "dev", "aws_account_id": "000000000000",
                       "aws_region": "us-east-1", "cell_id": "fixture-cell",
                       "existing_db_resource_id": "cluster-FIXTURE123", "database_name": "devlake_cell",
                       "ca_configmap_name": "rds-ca"}
        self.api_image = "registry.example.invalid/api@sha256:" + "a" * 64
        self.worker_image = "registry.example.invalid/worker@sha256:" + "b" * 64
        self.variables = {"aws_account_id": self.config["aws_account_id"],
                          "aws_region": self.config["aws_region"], "environment": "dev",
                          "cell_id": "fixture-cell", "existing_db_resource_id": "cluster-FIXTURE123",
                          "worker_image": self.worker_image, "schema_manifest_sha256": "c" * 64}
        self.record = {"artifacts": {
            "api": {"repository_uri": "registry.example.invalid/api", "digest": "sha256:" + "a" * 64},
            "worker": {"repository_uri": "registry.example.invalid/worker", "digest": "sha256:" + "b" * 64}}}
        self.scope = ("pilot", "protected-table", "000000000000", "us-east-1",
                      "finalizer", "publisher", "label")
        connection = mock.patch.dict(os.environ, {"ADP_DEVLAKE_INTENT_CONNECTION_LABEL": "label"})
        connection.start()
        self.addCleanup(connection.stop)
        self.writes = 0
        self.finalize = mock.Mock(get_registered=mock.Mock(side_effect=self.read),
                                  save=mock.Mock(side_effect=self.save))

    def read(self, role, label_override=None):
        self.assertEqual((role, label_override), ("finalizer", "label"))
        self.ledger.validate(self.record.get("optional_resources", {}), self.scope[2],
                             self.scope[3], self.record["artifacts"])
        return self.scope, json.dumps(self.record, sort_keys=True), copy.deepcopy(self.record)

    def save(self, scope, previous, record):
        self.assertEqual(scope, self.scope)
        if previous != json.dumps(self.record, sort_keys=True):
            raise ValueError("concurrent ledger change")
        self.record = copy.deepcopy(record)
        self.writes += 1

    def test_collection_retry_keeps_one_protected_intent_and_retains_dependencies(self):
        intent = self.ledger.collection(self.config, self.variables)
        self.assertEqual(self.ledger.register(self.finalize, "collection:fixture-cell", intent), intent)
        self.assertEqual(self.ledger.register(self.finalize, "collection:fixture-cell", intent), intent)
        self.assertEqual(self.writes, 1)
        self.assertEqual(intent["retained"]["db_resource_id"], "cluster-FIXTURE123")
        self.assertNotIn("db_resource_id", intent["owned"])
        self.assertEqual(intent["owned"]["iam_role_policy"],
                         "adp-dev-insights-devlake-fixture-cell-worker/cell-db-connect")

    def test_conflict_and_terminal_request_refuse_registration(self):
        intent = self.ledger.collection(self.config, self.variables)
        self.ledger.register(self.finalize, "collection:fixture-cell", intent)
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            self.ledger.register(self.finalize, "collection:fixture-cell",
                                 self.ledger.collection({**self.config, "database_name": "other"}, self.variables))
        self.record["finalization_requested"] = {"reason": "abort"}
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.ledger.register(self.finalize, "collection:fixture-cell", intent)
        self.assertEqual(self.writes, 1)

    def test_separate_cells_have_separate_state_and_resources(self):
        first = self.ledger.collection(self.config, self.variables)
        second_config = {**self.config, "cell_id": "other-cell"}
        second_variables = {**self.variables, "cell_id": "other-cell"}
        second = self.ledger.collection(second_config, second_variables)
        self.ledger.register(self.finalize, "collection:fixture-cell", first)
        self.ledger.register(self.finalize, "collection:other-cell", second)
        self.assertNotEqual(first["state_key"], second["state_key"])
        self.assertTrue(set(first["owned"].values()).isdisjoint(second["owned"].values()))
        self.assertEqual(self.writes, 2)

    def test_conflicting_state_and_owned_name_refuse_before_write(self):
        first = self.ledger.collection(self.config, self.variables)
        self.ledger.register(self.finalize, "collection:fixture-cell", first)
        second = self.ledger.collection({**self.config, "cell_id": "other-cell"},
                                        {**self.variables, "cell_id": "other-cell"})
        for field in ("state_key", "owned"):
            duplicate = {**second, field: first[field]}
            with self.subTest(field=field), mock.patch.object(
                    self.ledger, "collection", side_effect=lambda config, variables:
                    duplicate if config["cell_id"] == "other-cell" else first):
                with self.assertRaisesRegex(ValueError, "share"):
                    self.ledger.register(self.finalize, "collection:other-cell", duplicate)
        self.assertEqual(self.writes, 1)

    def test_lost_save_response_requires_matching_consistent_readback(self):
        intent = self.ledger.collection(self.config, self.variables)

        def saved_then_lost(scope, previous, record):
            self.save(scope, previous, record)
            raise ValueError("response lost")

        self.finalize.save.side_effect = saved_then_lost
        self.ledger.register(self.finalize, "collection:fixture-cell", intent)
        self.assertEqual(self.writes, 1)
        self.record.pop("optional_resources")
        self.finalize.save.side_effect = ValueError("update rejected")
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            self.ledger.register(self.finalize, "collection:fixture-cell", intent)

    def test_connector_only_intent_needs_no_database(self):
        connector = self.ledger.connectors(self.config, self.api_image)
        self.ledger.register(self.finalize, "connectors", connector)
        self.assertEqual(connector["retained"], {})
        self.assertEqual(connector["state_key"], "dev/modules/connectors/devlake/terraform.tfstate")
        self.assertEqual(self.writes, 1)

    def test_installer_registers_before_any_terraform(self):
        for script, name in (("deploy.py", "collection"), ("deploy_connectors.py", "connectors")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                spec = importlib.util.spec_from_file_location("fixture_" + name, MODULE / script)
                installer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(installer)
                calls = []
                config = {**self.config, "deployment_label": "fixture", "deployment_role": "role"}
                variables = self.variables if name == "collection" else {"api_image": self.api_image}
                with mock.patch.object(installer, "selected_config", return_value=config), \
                        mock.patch.object(installer, "checked_install", return_value=variables), \
                        mock.patch.object(installer.intent_ledger, "register",
                                          side_effect=lambda *_: calls.append("register")), \
                        mock.patch.object(installer.deploy if name == "connectors" else installer,
                                          "terraform_run", side_effect=lambda *args, **kwargs: calls.append("terraform")), \
                        mock.patch.object(sys, "argv", [script, "--config", str(Path(directory) / "private"),
                                                         "--plan-file", str(Path(directory) / "plan")]):
                    installer.main()
                self.assertEqual(calls, ["register", "terraform"])

    def test_connector_selection_disabled_and_invalid_inputs(self):
        spec = importlib.util.spec_from_file_location("fixture_connector_config", MODULE / "deploy_connectors.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text('{"enabled":false}')
            path.chmod(0o600)
            self.assertIsNone(installer.selected_config(path))
            path.write_text('{"enabled":"false"}')
            with self.assertRaisesRegex(ValueError, "explicit boolean"):
                installer.selected_config(path)

    def test_connector_selected_config_requires_scoped_boundary_and_cidr(self):
        spec = importlib.util.spec_from_file_location("fixture_connector_selection", MODULE / "deploy_connectors.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        config = {"enabled": True, "environment": "dev", "aws_account_id": "000000000000",
                  "aws_region": "us-east-1", "deployment_label": "fixture-deployer",
                  "deployment_role": "approved-deployer", "api_receipt": "/private/api.json",
                  "api_role_boundary_arn": "arn:aws:iam::000000000000:policy/api-boundary",
                  "provider_egress_cidrs": ["198.51.100.0/24"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for candidate, permitted in ((config, True),
                                         ({**config, "api_role_boundary_arn": "arn:aws:iam::111111111111:policy/other"}, False),
                                         ({**config, "provider_egress_cidrs": ["not-a-cidr"]}, False)):
                path.write_text(json.dumps(candidate))
                path.chmod(0o600)
                if permitted:
                    self.assertEqual(installer.selected_config(path), candidate)
                else:
                    with self.assertRaisesRegex(ValueError, "boundary or provider egress"):
                        installer.selected_config(path)

    def test_finalizer_label_override_requires_finalizer_role(self):
        import finalize

        with mock.patch.object(finalize, "settings", return_value=self.scope), \
                mock.patch.object(finalize, "authenticate") as authenticate, \
                mock.patch.object(finalize, "protected_table"), \
                mock.patch.object(finalize, "load", return_value=("previous", self.record)), \
                mock.patch.object(finalize, "validate"):
            self.assertEqual(finalize.get_registered("finalizer", label_override="intent-label")[-1], self.record)
            authenticate.assert_called_once_with("000000000000", "finalizer", "intent-label")
            with self.assertRaisesRegex(ValueError, "finalizer connection"):
                finalize.get_registered("publisher", label_override="intent-label")

    def test_connector_plan_uses_independent_root_and_registered_state_key(self):
        import deploy

        connector = self.ledger.connectors(self.config, self.api_image)
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "review.tfplan"
            config = {**self.config, "deployment_label": "fixture-deployer"}
            with mock.patch.object(deploy.subprocess, "run") as terraform:
                deploy.terraform_run(config, {"enabled": True, "api_image": self.api_image}, plan,
                                     infra=ROOT / "modules/connectors/devlake/infra",
                                     state_key=connector["state_key"])
            commands = [call.args[0] for call in terraform.call_args_list]
            self.assertEqual(len(commands), 2)
            self.assertIn("-chdir=" + str(ROOT / "modules/connectors/devlake/infra"), commands[0])
            self.assertIn("-backend-config=key=" + connector["state_key"], commands[0])
            self.assertEqual(commands[1][9], "plan")

    def test_missing_intent_connection_refuses_before_ledger_access(self):
        with mock.patch.dict(os.environ, {"ADP_DEVLAKE_INTENT_CONNECTION_LABEL": ""}):
            with self.assertRaisesRegex(ValueError, "finalizer connection"):
                self.ledger.register(self.finalize, "connectors",
                                     self.ledger.connectors(self.config, self.api_image))
        self.finalize.get_registered.assert_not_called()

    def test_unqualified_cleanup_blocks_both_installer_apply_paths_before_terraform(self):
        import deploy

        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "review.tfplan"
            plan.write_bytes(b"reviewed")
            plan.chmod(0o600)
            digest = hashlib.sha256(plan.read_bytes()).hexdigest()
            with mock.patch.object(deploy.subprocess, "run") as terraform:
                for infra, state_key in ((deploy.INFRA, self.ledger.collection(self.config, self.variables)["state_key"]),
                                         (ROOT / "modules/connectors/devlake/infra",
                                          self.ledger.connectors(self.config, self.api_image)["state_key"])):
                    with self.subTest(state_key=state_key):
                        with self.assertRaisesRegex(ValueError, "optional_cleanup_required"):
                            deploy.terraform_run({**self.config, "deployment_label": "fixture"},
                                                 {"enabled": True}, plan, apply=True,
                                                 expected_plan_sha256=digest, infra=infra,
                                                 state_key=state_key)
                terraform.assert_not_called()


if __name__ == "__main__":
    unittest.main()
