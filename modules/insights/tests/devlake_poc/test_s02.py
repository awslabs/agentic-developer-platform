import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake/admission.py"


class ImageAdmissionTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_admission", MODULE)
        self.admission = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.admission)
        self.account = "000000000000"
        self.region = "us-east-1"
        self.revision = "a" * 40
        self.schema_digest = hashlib.sha256(self.admission.SCHEMA.read_bytes()).hexdigest()
        self.receipts = {}
        self.artifacts = {}
        for component in ("api", "worker"):
            uri = f"{self.account}.dkr.ecr.{self.region}.amazonaws.com/devlake-poc/{component}"
            digest = "sha256:" + ("b" if component == "api" else "c") * 64
            tag = self.revision + "-" + component
            self.receipts[component] = {
                "component": component, "revision": self.revision,
                "image_digest": digest, "repository_uri": uri,
                "schema_sha256": self.schema_digest, "sbom_sha256": "d" * 64,
            }
            self.artifacts[component] = {
                "repository_uri": uri, "digest": digest, "tag": tag, "state": "accepted",
            }
        self.record = {"operator": "fixture-operator", "finalizer_revision": self.revision,
                       "artifacts": self.artifacts}
        self.scope = ("fixture-pilot", "fixture-table", self.account, self.region,
                      "finalizer-role", "publisher-role", "fixture-label")
        self.calls = []
        patch = mock.patch.dict(os.environ, {"GITHUB_ACTOR": "fixture-operator"})
        patch.start()
        self.addCleanup(patch.stop)
        for name, replacement in (
            ("get_registered", lambda role: (self.scope, "previous", self.record)),
            ("preflight", lambda *arguments: self.calls.append(("finalizer", *arguments))),
            ("aws", self.readback),
        ):
            patch = mock.patch.object(self.admission.finalize, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def readback(self, label, service, operation, *arguments):
        self.calls.append((label, service, operation, *arguments))
        component = arguments[arguments.index("--repository-name") + 1].split("/")[-1]
        artifact = self.artifacts[component]
        return {"images": [{"registryId": self.account,
                            "repositoryName": "devlake-poc/" + component,
                            "imageId": {"imageDigest": artifact["digest"], "imageTag": artifact["tag"]}}],
                "failures": []}

    def test_checked_images_require_ledger_and_independent_readback(self):
        resolved = self.admission.image_preflight(self.receipts["api"], self.receipts["worker"])
        self.assertEqual(resolved["worker"], self.artifacts["worker"]["repository_uri"] + "@" +
                         self.artifacts["worker"]["digest"])
        self.assertEqual([call[0] for call in self.calls],
                         ["finalizer", "fixture-label", "finalizer", "fixture-label"])

    def test_connector_only_admission_reads_only_api_artifact(self):
        resolved = self.admission.image_preflight(self.receipts["api"])
        self.assertEqual(resolved, {"api": self.receipts["api"]["repository_uri"] + "@" +
                                    self.receipts["api"]["image_digest"]})
        self.assertEqual([call[0] for call in self.calls], ["finalizer", "fixture-label"])

    def test_missing_or_changed_receipt_fails_before_registry_access(self):
        for field, value in (("schema_sha256", "0" * 64), ("image_digest", "sha256:" + "0" * 64),
                             ("sbom_sha256", "oops")):
            with self.subTest(field=field):
                receipt = {**self.receipts["worker"], field: value}
                with self.assertRaises(ValueError):
                    self.admission.image_preflight(self.receipts["api"], receipt)
                self.assertEqual(self.calls, [])

    def test_finalizer_or_wrong_operator_rejects_install(self):
        self.record["finalization_requested"] = {"reason": "abort"}
        with self.assertRaisesRegex(ValueError, "active registered operator"):
            self.admission.image_preflight(self.receipts["api"], self.receipts["worker"])
        self.assertEqual(self.calls, [])

    def test_stale_finalizer_probes_refuse_before_readback(self):
        with mock.patch.object(self.admission.finalize, "preflight",
                               side_effect=ValueError("recent scheduled and manual probes are required")):
            with self.assertRaisesRegex(ValueError, "recent scheduled and manual probes"):
                self.admission.image_preflight(self.receipts["api"], self.receipts["worker"])
        self.assertEqual(self.calls, [])

    def test_mismatched_intent_and_registry_readback_fail(self):
        self.artifacts["worker"]["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "protected resource intent"):
            self.admission.image_preflight(self.receipts["api"], self.receipts["worker"])
        self.artifacts["worker"]["digest"] = self.receipts["worker"]["image_digest"]
        with mock.patch.object(self.admission.finalize, "aws", return_value={"images": [], "failures": []}):
            with self.assertRaisesRegex(ValueError, "independent ECR"):
                self.admission.image_preflight(self.receipts["api"], self.receipts["worker"])


class MigrationReceiptTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_migration_receipt", MODULE.parent / "migration_receipt.py")
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ca_path = Path(temporary.name) / "rds-ca.pem"
        self.ca_path.write_text("fixture-ca")
        self.target = dict(host="fixture.cluster-abc.us-east-1.rds.amazonaws.com", database="devlake_cell",
                           cell="fixture-cell", resource_id="cluster-FIXTURE123", username="migration_owner", ca_path=self.ca_path,
                           region="us-east-1", account="000000000000", role="migration-role", label="fixture")

    def test_current_unprovisioned_schema_rejects_before_database_access(self):
        reader = mock.Mock()
        old_manifest = self.ca_path.parent / "v1.json"
        old_manifest.write_text('{"schema_version": 1}')
        with mock.patch.object(self.migration, "SCHEMA", old_manifest):
            with self.assertRaisesRegex(ValueError, "migration_required"):
                self.migration.verify_migrated(reader, **self.target)
        reader.assert_not_called()

    def test_applied_manifest_requires_exact_database_digest(self):
        import json

        ready_manifest = self.ca_path.parent / "schema-manifest.json"
        manifest = json.loads(self.migration.SCHEMA.read_text())
        ready_manifest.write_text(json.dumps(manifest))
        digest = hashlib.sha256(ready_manifest.read_bytes()).hexdigest()
        with mock.patch.object(self.migration, "SCHEMA", ready_manifest):
            self.assertEqual(self.migration.verify_migrated(lambda **_: digest, **self.target), digest)
            with self.assertRaisesRegex(ValueError, "migration_required"):
                self.migration.verify_migrated(lambda **_: "0" * 64, **self.target)

    def test_fresh_iam_and_full_tls_are_used_for_each_physical_readback(self):
        def broker(label, *command):
            if command[:3] == ("aws", "sts", "get-caller-identity"):
                return '{"Account":"000000000000","Arn":"arn:aws:sts::000000000000:assumed-role/migration-role/fixture"}'
            self.assertEqual(command[:3], ("aws", "rds", "generate-db-auth-token"))
            return "fixture-iam-token"

        def sql_process(command, **kwargs):
            self.assertEqual(command[0], "psql")
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
            self.assertEqual(kwargs["env"]["PGSSLROOTCERT"], str(self.ca_path))
            self.assertEqual(kwargs["env"]["PGPASSWORD"], "fixture-iam-token")
            self.assertNotIn("PGHOST", kwargs["env"])
            return mock.Mock(stdout="a" * 64 + "\n")

        with mock.patch.object(self.migration.publish, "broker", side_effect=broker) as token, \
                mock.patch.object(self.migration.subprocess, "run", side_effect=sql_process), \
                mock.patch.dict(os.environ, {"PGHOST": "other.example.invalid"}):
            self.assertEqual(self.migration.readback(**self.target), "a" * 64)
        self.assertEqual(token.call_count, 2)

    def test_new_database_connections_renew_tokens_after_fifteen_minutes(self):
        clock_seconds = [0]
        minted_at = {}
        physical_connections = []

        def broker(label, *command):
            self.assertEqual(label, "fixture")
            if command[:3] == ("aws", "sts", "get-caller-identity"):
                return '{"Account":"000000000000","Arn":"arn:aws:sts::000000000000:assumed-role/migration-role/fixture"}'
            self.assertEqual(command[:3], ("aws", "rds", "generate-db-auth-token"))
            token = "issued-token-" + str(len(minted_at) + 1)
            minted_at[token] = clock_seconds[0]
            return token

        def physical_connection(command, **kwargs):
            self.assertEqual(command[0], "psql")
            token = kwargs["env"]["PGPASSWORD"]
            self.assertLess(clock_seconds[0] - minted_at[token], 15 * 60)
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
            self.assertEqual(kwargs["env"]["PGSSLROOTCERT"], str(self.ca_path))
            self.assertNotIn("PGHOST", kwargs["env"])
            physical_connections.append(token)
            return mock.Mock(stdout="a" * 64 + "\n")

        with mock.patch.object(self.migration.publish, "broker", side_effect=broker), \
                mock.patch.object(self.migration.subprocess, "run", side_effect=physical_connection), \
                mock.patch.dict(os.environ, {"PGPASSWORD": "expired-static-token", "PGHOST": "untrusted.invalid"}):
            self.assertEqual(self.migration.readback(**self.target), "a" * 64)
            clock_seconds[0] += 16 * 60
            self.assertEqual(self.migration.readback(**self.target), "a" * 64)
        self.assertEqual(physical_connections, ["issued-token-1", "issued-token-2"])

    def test_wrong_target_or_role_fails_before_sql(self):
        with mock.patch.object(self.migration.publish, "broker") as broker:
            with self.assertRaisesRegex(ValueError, "registered database target"):
                self.migration.readback(**{**self.target, "host": "other.example.invalid"})
            broker.assert_not_called()
            broker.return_value = '{"Account":"000000000000","Arn":"arn:aws:sts::000000000000:assumed-role/wrong-role/fixture"}'
            with self.assertRaisesRegex(ValueError, "selected migration role"):
                self.migration.readback(**self.target)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for the offline TLS fixture")
    def test_verified_tls_rejects_foreign_ca_and_wrong_hostname_without_retry(self):
        directory = self.ca_path.parent
        hostname = self.target["host"]
        leaf = directory / "server.pem"
        csr = directory / "server.csr"
        unrelated_ca = directory / "foreign-ca.pem"
        extensions = directory / "server.ext"
        extensions.write_text(f"subjectAltName=DNS:{hostname}\nextendedKeyUsage=serverAuth\n")
        native_run = subprocess.run

        def certificate_authority(path, key):
            native_run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=fixture-root", "-addext", "basicConstraints=critical,CA:TRUE",
                        "-keyout", str(key), "-out", str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        certificate_authority(self.ca_path, directory / "trusted-key.pem")
        certificate_authority(unrelated_ca, directory / "foreign-key.pem")
        native_run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={hostname}",
                    "-keyout", str(directory / "server-key.pem"), "-out", str(csr)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        native_run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(self.ca_path),
                    "-CAkey", str(directory / "trusted-key.pem"), "-CAcreateserial", "-days", "1",
                    "-extfile", str(extensions), "-out", str(leaf)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        sql_calls = []

        def sql_process(command, **kwargs):
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
            self.assertNotIn("PGHOST", kwargs["env"])
            sql_calls.append(command)
            result = native_run(["openssl", "verify", "-CAfile", kwargs["env"]["PGSSLROOTCERT"],
                                 "-verify_hostname", command[command.index("--host") + 1], str(leaf)],
                                capture_output=True, text=True)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, command)
            return mock.Mock(stdout="a" * 64 + "\n")

        def broker(label, *command):
            if command[:3] == ("aws", "sts", "get-caller-identity"):
                return '{"Account":"000000000000","Arn":"arn:aws:sts::000000000000:assumed-role/migration-role/fixture"}'
            return "fixture-iam-token"

        with mock.patch.object(self.migration.publish, "broker", side_effect=broker), \
                mock.patch.object(self.migration.subprocess, "run", side_effect=sql_process):
            self.assertEqual(self.migration.readback(**self.target), "a" * 64)
            with self.assertRaisesRegex(ValueError, "IAM/TLS migration readback failed"):
                self.migration.readback(**{**self.target, "ca_path": unrelated_ca})
            with self.assertRaisesRegex(ValueError, "IAM/TLS migration readback failed"):
                self.migration.readback(**{**self.target, "host": "wrong.cluster-abc.us-east-1.rds.amazonaws.com"})
        self.assertEqual(len(sql_calls), 3)


class OrderedAdmissionTest(unittest.TestCase):
    def test_migration_readback_precedes_image_and_finalizer_access(self):
        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_admission_order", MODULE)
        admission = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(admission)
        images = mock.Mock()
        with mock.patch.object(admission.migration_receipt, "verify_migrated",
                               side_effect=ValueError("migration_required")), \
                mock.patch.object(admission, "image_preflight", images):
            with self.assertRaisesRegex(ValueError, "migration_required"):
                admission.installation_preflight({}, {}, {})
        images.assert_not_called()


class OptionalInstallerTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_deploy", MODULE.parent / "deploy.py")
        self.deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.deploy)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = {
            "enabled": True, "connectors_enabled": True, "environment": "dev",
            "aws_account_id": "000000000000", "aws_region": "us-east-1",
            "deployment_label": "fixture-deployer", "deployment_role": "approved-deployer",
            "migration_label": "fixture-migrator", "migration_role": "approved-migrator",
            "cell_id": "fixture-cell", "existing_db_endpoint": "fixture.cluster-abc.us-east-1.rds.amazonaws.com",
            "existing_db_resource_id": "cluster-FIXTURE123", "database_name": "devlake_cell",
            "migration_username": "migration_owner", "ca_path": str(self.directory / "ca.pem"),
            "ca_configmap_name": "rds-ca", "worker_role_boundary_arn": "arn:aws:iam::000000000000:policy/approved-worker",
            "writer_db_username": "fixture_writer", "reader_db_username": "fixture_reader",
            "api_receipt": str(self.directory / "api.json"), "worker_receipt": str(self.directory / "worker.json"),
        }

    def test_missing_or_false_selection_skips_before_credentials(self):
        self.assertIsNone(self.deploy.selected_config(None))
        path = self.directory / "config.json"
        path.write_text('{"enabled": false}')
        path.chmod(0o600)
        self.assertIsNone(self.deploy.selected_config(path))
        path.write_text('{"enabled": "true"}')
        with self.assertRaisesRegex(ValueError, "explicit boolean"):
            self.deploy.selected_config(path)

    def test_install_checks_role_and_migration_before_resolving_images(self):
        expected_role = '{"Account":"000000000000","Arn":"arn:aws:sts::000000000000:assumed-role/approved-deployer/fixture"}'
        with mock.patch.object(self.deploy.publish, "broker", return_value=expected_role), \
                mock.patch.object(self.deploy.admission, "load_receipt", return_value={}), \
                mock.patch.object(self.deploy.admission, "installation_preflight",
                                  side_effect=ValueError("migration_required")) as preflight:
            with self.assertRaisesRegex(ValueError, "migration_required"):
                self.deploy.checked_install(self.config)
            self.assertEqual(preflight.call_args.args[-1]["resource_id"], "cluster-FIXTURE123")
        with mock.patch.object(self.deploy.publish, "broker", return_value=expected_role.replace("approved-deployer", "wrong-role")), \
                mock.patch.object(self.deploy.admission, "load_receipt") as receipt:
            with self.assertRaisesRegex(ValueError, "selected deployment role"):
                self.deploy.checked_install(self.config)
            receipt.assert_not_called()

    def test_reviewed_plan_is_bound_to_exact_variables_and_state(self):
        import json

        variables = {"enabled": True, "cell_id": "fixture-cell"}
        plan = self.directory / "reviewed.tfplan"
        plan.write_bytes(b"operator-reviewed-plan")
        plan.chmod(0o600)
        digest = hashlib.sha256(plan.read_bytes()).hexdigest()
        calls = []

        def terraform(command, **kwargs):
            self.assertEqual(command[:8], ["adp-cred", "assume", "--service", "aws", "--label",
                                            "fixture-deployer", "--exec", "terraform"])
            calls.append(command)
            if "show" in command:
                return mock.Mock(stdout=json.dumps({"variables": {
                    key: {"value": value} for key, value in variables.items()}}))
            return mock.Mock(stdout="")

        with mock.patch.object(self.deploy.subprocess, "run", side_effect=terraform), \
                mock.patch.object(self.deploy.intent_ledger, "require_cleanup_authority"):
            self.deploy.terraform_run(self.config, variables, plan, apply=True, expected_plan_sha256=digest)
        self.assertIn("-backend-config=key=dev/modules/insights/devlake/cells/fixture-cell/terraform.tfstate", calls[0])
        self.assertEqual([command[9] for command in calls], ["init", "show", "apply"])
        with mock.patch.object(self.deploy.subprocess, "run") as command:
            with self.assertRaisesRegex(ValueError, "plan digest differs"):
                self.deploy.terraform_run(self.config, variables, plan, apply=True,
                                          expected_plan_sha256="0" * 64)
            command.assert_not_called()


class InstallerEntrypointTest(unittest.TestCase):
    def test_selected_install_rejects_unprovisioned_manifest_before_terraform(self):
        import contextlib
        import io
        import json
        import tempfile

        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_entrypoint", MODULE.parent / "deploy.py")
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "private.json"
            config = dict.fromkeys(deploy.REQUIRED, "fixture")
            config.update(enabled=True, connectors_enabled=True, environment="dev",
                          aws_account_id="000000000000", aws_region="us-east-1",
                          deployment_label="fixture-deployer", deployment_role="approved-deployer",
                          existing_db_endpoint="fixture.cluster-abc.us-east-1.rds.amazonaws.com",
                          existing_db_resource_id="cluster-FIXTURE123", cell_id="fixture-cell",
                          migration_username="fixture_migrator", writer_db_username="fixture_writer",
                          reader_db_username="fixture_reader")
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            identity = '{"Account":"000000000000","Arn":"arn:aws:sts::000000000000:assumed-role/approved-deployer/fixture"}'
            with mock.patch.object(deploy.publish, "broker", return_value=identity), \
                    mock.patch.object(deploy.admission.migration_receipt, "checked_manifest", side_effect=ValueError("migration_required")), \
                    mock.patch.object(deploy.admission, "load_receipt", return_value={}), \
                    mock.patch.object(deploy, "terraform_run") as terraform, \
                    mock.patch.object(sys, "argv", ["deploy.py", "--config", str(config_path),
                                                    "--plan-file", str(Path(directory) / "review.tfplan")]), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as failure:
                    deploy.main()
                self.assertEqual(failure.exception.code, 2)
                self.assertIn("migration_required", errors.getvalue())
                terraform.assert_not_called()

    def test_ready_schema_gate_precedes_image_and_plan(self):
        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_ready_admission", MODULE)
        admission = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(admission)
        receipt = {"schema_sha256": "a" * 64}
        calls = []
        with mock.patch.object(admission.migration_receipt, "verify_migrated",
                               side_effect=lambda *args, **kwargs: (calls.append("migration") or "a" * 64)), \
                mock.patch.object(admission, "image_preflight",
                                  side_effect=lambda *args: (calls.append("images") or {"worker": "pinned"})):
            self.assertEqual(admission.installation_preflight(receipt, receipt, {})["worker"], "pinned")
        self.assertEqual(calls, ["migration", "images"])


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile

        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_schema_migrate", MODULE.parent / "migrate.py")
        self.migrate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migrate)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.sql_path = self.directory / "schema-v2.sql"
        self.sql_path.write_text("SELECT pg_advisory_xact_lock(10);\n")
        self.schema_path = self.directory / "schema-manifest.json"
        manifest = json.loads(self.migrate.migration_receipt.SCHEMA.read_text())
        manifest['migration_sql_sha256'] = hashlib.sha256(self.sql_path.read_bytes()).hexdigest()
        self.schema_path.write_text(json.dumps(manifest))
        self.schema_digest = hashlib.sha256(self.schema_path.read_bytes()).hexdigest()
        self.ca_path = self.directory / "rds-ca.pem"
        self.ca_path.write_text("fixture-ca")
        self.config = {
            "existing_db_endpoint": "fixture.cluster-abc.us-east-1.rds.amazonaws.com",
            "existing_db_resource_id": "cluster-FIXTURE123", "database_name": "devlake_cell",
            "cell_id": "fixture-cell", "migration_username": "migration_owner",
            "writer_db_username": "fixture_writer", "reader_db_username": "fixture_reader",
            "ca_path": str(self.ca_path), "aws_region": "us-east-1", "aws_account_id": "000000000000",
            "migration_role": "migration-role", "migration_label": "fixture-migrator",
            "api_receipt": str(self.directory / "api.json"), "worker_receipt": str(self.directory / "worker.json"),
        }

    def test_v1_manifest_refuses_before_any_published_image_or_sql(self):
        old_manifest = self.directory / "v1.json"
        old_manifest.write_text('{"schema_version": 1}')
        with mock.patch.object(self.migrate.migration_receipt, "SCHEMA", old_manifest), \
                mock.patch.object(self.migrate.admission, "image_preflight") as images, \
                mock.patch.object(self.migrate.publish, "broker") as broker, \
                mock.patch.object(self.migrate.subprocess, "run") as database:
            with self.assertRaisesRegex(ValueError, "migration_required"):
                self.migrate.apply_migration(self.config)
            images.assert_not_called()
            broker.assert_not_called()
            database.assert_not_called()

    def test_incomplete_inventory_cannot_produce_receipt(self):
        import json

        original = json.loads(self.schema_path.read_text())
        for change in ({"staging": {**original["staging"], "tables": []}},
                       {"staging": {**original["staging"], "generation_guards": []}},
                       {"staging": {**original["staging"], "generation_guards": [
                           table for table in original["staging"]["generation_guards"] if table != "_tool_slack_channel_messages"]}},
                       {"staging": {**original["staging"], "generation_guards": [
                           table for table in original["staging"]["generation_guards"] if table != "cq_issues"]}},
                       {"raw": {"tables": []}}, {"raw": {"tables": ["_raw_foreign_table"]}},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_slack_channel_message"]}},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_sonarqube_api_issues"]}},
                       {"tool": ["_tool_github_repos"]},
                       {"tool": [table for table in original["tool"] if table != "_tool_slack_channel_messages"]},
                       {"tool": [table for table in original["tool"] if table != "_tool_sonarqube_issue_code_blocks"]},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_jenkins_api_builds"]}},
                       {"tool": [table for table in original["tool"] if table != "_tool_jenkins_build_commits"]},
                       {"domain": [table for table in original["domain"] if table != "cicd_tasks"]},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_argocd_api_sync_operations"]}},
                       {"tool": [table for table in original["tool"] if table != "_tool_argocd_revision_images"]},
                       {"domain": [table for table in original["domain"] if table != "cicd_deployment_commits"]},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_gitlab_api_pipeline_details"]}},
                       {"tool": [table for table in original["tool"] if table != "_tool_gitlab_pipelines"]},
                       {"tool": [table for table in original["tool"] if table != "_devlake_subtask_states"]},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_pagerduty_incidents"]}},
                       {"raw": {**original["raw"], "tables": [
                           table for table in original["raw"]["tables"] if table != "_raw_linear_issues"]}},
                       {"tool": [table for table in original["tool"] if table != "_tool_pagerduty_assignments"]},
                       {"tool": [table for table in original["tool"] if table != "_tool_linear_issue_labels"]},
                       {"domain": [table for table in original["domain"] if table != "board_issues"]},
                       {"staging": {**original["staging"], "generation_guards": [
                           table for table in original["staging"]["generation_guards"] if table != "cicd_pipelines"]}},
                       {"domain": [table for table in original["domain"] if table != "cq_issues"]},
                       {"migration_sql_sha256": "not-a-digest"}):
            with self.subTest(change=change):
                self.schema_path.write_text(json.dumps({**original, **change}))
                with mock.patch.object(self.migrate.migration_receipt, "SCHEMA", self.schema_path), \
                        mock.patch.object(self.migrate.admission, "image_preflight") as images, \
                        mock.patch.object(self.migrate.subprocess, "run") as database:
                    with self.assertRaisesRegex(ValueError, "migration_required"):
                        self.migrate.apply_migration(self.config, self.sql_path)
                    images.assert_not_called()
                    database.assert_not_called()

    def test_pinned_schema_migrates_atomically_then_reads_back_receipt(self):
        import json

        commands = []

        def broker(label, *arguments):
            commands.append(arguments[1:3])
            if arguments[1] == "sts":
                return json.dumps({"Account": "000000000000",
                                   "Arn": "arn:aws:sts::000000000000:assumed-role/migration-role/fixture"})
            return "fresh-iam-token"

        def transaction(command, **kwargs):
            self.assertIn("--single-transaction", command)
            self.assertEqual(command[-2:], ["--file", "-"])
            self.assertIn("--set=cell_id=fixture-cell", command)
            self.assertIn("--set=db_resource_id=cluster-FIXTURE123", command)
            self.assertIn("--set=manifest_sha256=" + self.schema_digest, command)
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
            self.assertEqual(kwargs["env"]["PGPASSWORD"], "fresh-iam-token")
            self.assertIn('public."_raw_jira_api_boards"', kwargs["input"])
            commands.append("transaction")
            return mock.Mock()

        with mock.patch.object(self.migrate.migration_receipt, "SCHEMA", self.schema_path), \
                mock.patch.object(self.migrate.admission, "load_receipt", return_value={}), \
                mock.patch.object(self.migrate.admission, "image_preflight",
                                  side_effect=lambda *args: commands.append("finalizer")), \
                mock.patch.object(self.migrate.publish, "broker", side_effect=broker), \
                mock.patch.object(self.migrate.subprocess, "run", side_effect=transaction), \
                mock.patch.object(self.migrate.migration_receipt, "readback",
                                  side_effect=lambda **kwargs: (commands.append("readback") or self.schema_digest)):
            self.assertEqual(self.migrate.apply_migration(self.config, self.sql_path), self.schema_digest)
        self.assertEqual(commands, ["finalizer", ("sts", "get-caller-identity"),
                                    ("rds", "generate-db-auth-token"), "transaction", "readback"])

    def test_migration_transaction_and_receipt_use_separate_fresh_tokens(self):
        import json

        issued = []
        physical_connections = []

        def broker(label, *command):
            if command[:3] == ("aws", "sts", "get-caller-identity"):
                return json.dumps({"Account": "000000000000",
                                   "Arn": "arn:aws:sts::000000000000:assumed-role/migration-role/fixture"})
            self.assertEqual(command[:3], ("aws", "rds", "generate-db-auth-token"))
            issued.append("iam-token-" + str(len(issued) + 1))
            return issued[-1]

        def sql_process(command, **kwargs):
            physical_connections.append(kwargs["env"]["PGPASSWORD"])
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
            self.assertEqual(kwargs["env"]["PGSSLROOTCERT"], str(self.ca_path))
            return mock.Mock(stdout=self.schema_digest + "\n")

        with mock.patch.object(self.migrate.migration_receipt, "SCHEMA", self.schema_path), \
                mock.patch.object(self.migrate.admission, "load_receipt", return_value={}), \
                mock.patch.object(self.migrate.admission, "image_preflight"), \
                mock.patch.object(self.migrate.publish, "broker", side_effect=broker), \
                mock.patch.object(self.migrate.subprocess, "run", side_effect=sql_process):
            self.assertEqual(self.migrate.apply_migration(self.config, self.sql_path), self.schema_digest)
        self.assertEqual(physical_connections, ["iam-token-1", "iam-token-2"])

    def test_changed_sql_or_target_fails_before_database_mutation(self):
        with mock.patch.object(self.migrate.migration_receipt, "SCHEMA", self.schema_path), \
                mock.patch.object(self.migrate.admission, "image_preflight") as finalizer, \
                mock.patch.object(self.migrate.subprocess, "run") as transaction:
            self.sql_path.write_text("SELECT 1;\n")
            with self.assertRaisesRegex(ValueError, "checked schema"):
                self.migrate.apply_migration(self.config, self.sql_path)
            self.sql_path.write_text("SELECT pg_advisory_xact_lock(10);\n")
            with self.assertRaisesRegex(ValueError, "registered migration target"):
                self.migrate.apply_migration({**self.config, "existing_db_resource_id": "other;DROP"}, self.sql_path)
            finalizer.assert_not_called()
            transaction.assert_not_called()


def _ready_migration_fixture(test_case):
    schema_patch = mock.patch.object(test_case.migrate.migration_receipt, "SCHEMA", test_case.schema_path)
    image_patch = mock.patch.object(test_case.migrate.admission, "load_receipt", return_value={})
    preflight_patch = mock.patch.object(test_case.migrate.admission, "image_preflight")
    return schema_patch, image_patch, preflight_patch


class MigrationFailureTest(unittest.TestCase):
    def setUp(self):
        SchemaMigrationTest.setUp(self)

    def test_wrong_migration_role_refuses_before_transaction(self):
        import json

        patches = _ready_migration_fixture(self)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(self.migrate.publish, "broker", return_value=json.dumps({
                    "Account": "000000000000",
                    "Arn": "arn:aws:sts::000000000000:assumed-role/foreign-role/fixture"})), \
                mock.patch.object(self.migrate.subprocess, "run") as transaction:
            with self.assertRaisesRegex(ValueError, "selected migration role"):
                self.migrate.apply_migration(self.config, self.sql_path)
            transaction.assert_not_called()

    def test_failed_sql_has_no_successful_receipt(self):
        import json

        identity = json.dumps({"Account": "000000000000",
                               "Arn": "arn:aws:sts::000000000000:assumed-role/migration-role/fixture"})
        patches = _ready_migration_fixture(self)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(self.migrate.publish, "broker", side_effect=[identity, "fresh-iam-token"]), \
                mock.patch.object(self.migrate.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, "psql")), \
                mock.patch.object(self.migrate.migration_receipt, "readback") as receipt:
            with self.assertRaisesRegex(ValueError, "IAM/TLS migration transaction failed"):
                self.migrate.apply_migration(self.config, self.sql_path)
            receipt.assert_not_called()


class CellRolePolicyTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(MODULE.parent))
        self.addCleanup(lambda: sys.path.remove(str(MODULE.parent)))
        spec = importlib.util.spec_from_file_location("devlake_cell_grants", MODULE.parent / "cell_grants.py")
        self.grants = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.grants)
        self.config = {"database_name": "fixture_cell", "cell_id": "fixture-cell",
                       "existing_db_resource_id": "cluster-FIXTURE123", "migration_username": "fixture_migrator",
                       "writer_db_username": "fixture_writer", "reader_db_username": "fixture_reader"}
        self.manifest = {"raw": {"tables": ["_raw_jira_api_boards"]},
                         "tool": ["_tool_github_repos", "_tool_jira_boards"],
                         "domain": ["repos", "boards"]}

    def test_only_registered_cell_tables_receive_scoped_dml_and_read_grants(self):
        sql = self.grants.checked_roles(self.config, self.manifest, "a" * 64)
        self.assertIn("current_database() <> 'fixture_cell'", sql)
        self.assertIn("current_user <> 'fixture_migrator'", sql)
        self.assertIn("relowner <> (SELECT oid FROM pg_catalog.pg_roles", sql)
        self.assertIn("foreign identity already holds a cell table grant", sql)
        self.assertIn("granted.rolname <> 'rds_iam'", sql)
        self.assertIn("tgfoid = 'devlake_meta.require_current_generation()'::regprocedure", sql)
        self.assertIn("cell_id = 'fixture-cell' AND db_resource_id = 'cluster-FIXTURE123'", sql)
        self.assertIn("GRANT USAGE, SELECT ON SEQUENCE %s TO %I", sql)
        self.assertIn("foreign cell receipt exists in selected database", sql)
        self.assertIn('GRANT SELECT ON devlake_meta.schema_receipts, devlake_meta.collection_generations,', sql)
        self.assertNotIn('GRANT INSERT ON devlake_meta.schema_receipts', sql)
        self.assertIn('REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE "fixture_cell" FROM PUBLIC', sql)
        self.assertIn('GRANT SELECT, INSERT, UPDATE, DELETE ON public."_raw_jira_api_boards"', sql)
        self.assertIn('TO "fixture_writer";', sql)
        self.assertIn('GRANT SELECT ON public."_raw_jira_api_boards"', sql)
        self.assertIn('TO "fixture_reader";', sql)
        self.assertNotIn('GRANT CREATE', sql)
        self.assertNotIn('GRANT ALL', sql)
        self.assertNotIn('GRANT INSERT ON devlake_meta.', sql)
        self.assertNotIn('S3', sql)

    def test_wrong_target_and_role_aliases_fail_before_sql(self):
        for change in ({"writer_db_username": "fixture_reader"},
                       {"writer_db_username": "foreign\"; DROP ROLE fixture_writer;--"},
                       {"database_name": "protected;DROP DATABASE protected"},
                       {"cell_id": "foreign'; DROP TABLE protected;--"},
                       {"reader_db_username": None}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "three distinct roles"):
                self.grants.checked_roles({**self.config, **change}, self.manifest, "a" * 64)
