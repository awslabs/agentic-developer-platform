import importlib.util
import hashlib
import json
import os
import shutil
import socket
import sys
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
BUILD = ROOT / "modules/connectors/devlake/scripts/build.sh"
LOCK = ROOT / "modules/connectors/devlake/source-lock.json"


class SourceLockTest(unittest.TestCase):
    def invoke(self, lock, customization=None):
        if customization is None:
            customization = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "source-lock.json"
            lock_path.write_text(json.dumps(lock))
            return subprocess.run(
                ["bash", str(BUILD), "--lock", str(lock_path), "--customization", customization, "--check"],
                capture_output=True, text=True,
            )

    def test_audited_source_and_base_images_are_pinned(self):
        lock = json.loads(LOCK.read_text())
        self.assertEqual(self.invoke(lock).returncode, 0)
        self.assertEqual(lock["upstream"]["revision"], "6502e549f58eb40c40ccbcd39aab9bc8eea6616f")

    def test_reject_moving_ref_and_mutable_image(self):
        lock = json.loads(LOCK.read_text())
        lock["upstream"]["revision"] = "main"
        self.assertIn("commit SHA", self.invoke(lock).stderr)
        lock = json.loads(LOCK.read_text())
        lock["images"]["builder"] = "golang:latest"
        self.assertIn("immutable digests", self.invoke(lock).stderr)
        self.assertIn("commit SHA", self.invoke(json.loads(LOCK.read_text()), "HEAD").stderr)
        self.assertIn("does not match", self.invoke(json.loads(LOCK.read_text()), "a" * 40).stderr)

    def test_component_recipes_are_independent_and_default_off(self):
        for component, root, executable in (
            ("api", ROOT / "modules/connectors/devlake", "connector-api"),
            ("worker", ROOT / "modules/insights/poc/devlake", "collector-worker"),
        ):
            with self.subTest(component=component):
                recipe = (root / "Dockerfile").read_text()
                contract = json.loads((root / "module-contract.json").read_text())
                self.assertTrue(contract["default_enabled"] is False)
                self.assertIn("FROM ${BUILDER_IMAGE}", recipe)
                self.assertIn("FROM ${RUNTIME_IMAGE}", recipe)
                self.assertIn("/usr/local/bin/" + executable, recipe)
                for asset in ("LICENSE", "NOTICE", "schema-manifest.json", "source-manifest.json", "module-contract.json"):
                    self.assertIn("/src/adp/" + asset, recipe)

    def test_reject_uncommitted_component_input(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        with tempfile.NamedTemporaryFile(dir=ROOT / "modules/connectors/devlake", prefix="uncommitted-", suffix=".txt"):
            result = subprocess.run(
                [str(BUILD), "--customization", revision, "--component", "api",
                 "--engine", "docker", "--tag", f"localhost/devlake-api:{revision}"],
                capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted component sources", result.stderr)

    def test_scenario_refuses_dirty_component_sources(self):
        verifier = ROOT / "modules/insights/poc/devlake/scripts/verify-poc.sh"
        with tempfile.NamedTemporaryFile(dir=ROOT / "modules/connectors/devlake", prefix="dirty-source-"):
            result = subprocess.run([str(verifier)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("component sources must be committed", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_reject_missing_or_changed_patch(self):
        lock = json.loads(LOCK.read_text())
        lock["patches"] = []
        self.assertIn("patch inventory", self.invoke(lock).stderr)
        lock = json.loads(LOCK.read_text())
        lock["patches"][0]["sha256"] = "0" * 64
        self.assertIn("patch digest mismatch", self.invoke(lock).stderr)
        lock = json.loads(LOCK.read_text())
        lock["patches"][0]["path"] = "../other.patch"
        self.assertIn("unsafe patch inventory", self.invoke(lock).stderr)


@unittest.skipUnless(os.environ.get("DEVLAKE_SOURCE_INTEGRATION") == "1" and shutil.which("go"),
                     "set DEVLAKE_SOURCE_INTEGRATION=1 with Go 1.26 and upstream network access")
class CleanWorkspaceBuildTest(unittest.TestCase):
    def test_patch_conflict_aborts_before_image_and_recovers_with_clean_source(self):
        spec = importlib.util.spec_from_file_location("devlake_build", ROOT / "modules/connectors/devlake/scripts/build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        original_checkout = build.checkout

        def conflicting_checkout(lock, destination):
            original_checkout(lock, destination)
            collector = destination / "backend/helpers/pluginhelper/api/api_collector.go"
            original = collector.read_text()
            self.assertIn("rawTableAutoMigrateLock sync.Mutex", original)
            collector.write_text(original.replace("rawTableAutoMigrateLock sync.Mutex",
                                                  "rawTableAutoMigrateLock sync.RWMutex"))

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "conflicting-source"
            output = Path(temporary) / "refused-image"
            arguments = ["build.sh", "--customization", revision, "--component", "api",
                         "--output", str(output), "--destination", str(destination)]
            real_check_output = subprocess.check_output

            def checked_source(command, **kwargs):
                if command[:3] == ["git", "status", "--porcelain"]:
                    return ""
                return real_check_output(command, **kwargs)

            with mock.patch.object(build, "checkout", side_effect=conflicting_checkout), \
                    mock.patch.object(build.subprocess, "check_output", side_effect=checked_source), \
                    mock.patch.object(sys, "argv", arguments):
                with self.assertRaises(subprocess.CalledProcessError):
                    build.main()
            self.assertFalse(output.exists())
            self.assertFalse((destination / "backend/adp").exists())
            corrected = Path(temporary) / "corrected-source"
            subprocess.run([str(BUILD), "--customization", revision, "--destination", str(corrected)],
                           check=True)
            self.assertIn("raw table is not provisioned",
                          (corrected / "backend/helpers/pluginhelper/api/api_collector.go").read_text())

    def test_source_build(self):
        spec = importlib.util.spec_from_file_location("devlake_build", ROOT / "modules/connectors/devlake/scripts/build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        lock = build.read_lock(LOCK)
        self.assertIn("go1.26", subprocess.check_output(["go", "version"], text=True))
        schema = json.loads((ROOT / "modules/insights/poc/devlake/schema-manifest.json").read_text())
        self.assertEqual(schema["raw"]["table_rule"], "_raw_{table}")
        self.assertFalse(schema["raw"]["raw_collector_runtime_ddl"])
        scenario = json.loads(subprocess.check_output(
            [str(ROOT / "modules/insights/poc/devlake/scripts/verify-poc.sh")], cwd=ROOT, text=True))
        self.assertEqual(scenario["revision"], revision)
        self.assertEqual(set(scenario["images"]), {"api", "worker"})
        expected_schema_digest = hashlib.sha256(
            (ROOT / "modules/insights/poc/devlake/schema-manifest.json").read_bytes()).hexdigest()
        for component in ("api", "worker"):
            self.assertRegex(scenario["images"][component]["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(scenario["images"][component]["schema_sha256"], expected_schema_digest)
            self.assertEqual(scenario["images"][component]["upstream"], lock["upstream"]["revision"])
            self.assertEqual(scenario["images"][component]["platform"], lock["platform"])
        self.assertNotEqual(scenario["images"]["api"]["digest"], scenario["images"]["worker"]["digest"])
        for workspace_number in range(2):
            with self.subTest(workspace=workspace_number), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "upstream"
                subprocess.run([str(BUILD), "--customization", revision, "--destination", str(source)], check=True)
                for component in ("api", "worker"):
                    manifest = build.stage(lock, source, revision, component)
                    self.assertEqual(manifest["component"], component)
                    self.assertEqual(manifest["platform"], "linux/amd64")
                    self.assertEqual(manifest["upstream"]["revision"], lock["upstream"]["revision"])
                    binary = Path(temporary) / component
                    package = "adp-connector-api" if component == "api" else "adp-collector-worker"
                    subprocess.run(["go", "build", "-mod=readonly", "-trimpath", "-o", str(binary),
                                    "./cmd/" + package], cwd=source / "backend", check=True,
                                   env={**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"})
                    metadata = subprocess.check_output(["go", "version", "-m", str(binary)], text=True)
                    self.assertIn("go1.26", metadata)
                    self.assertIn("GOARCH=amd64", metadata)
                    symbols = subprocess.check_output(["go", "tool", "nm", str(binary)], text=True)
                    required = (("plugins/github/api.RemoteScopes", "plugins/jira/api.RemoteScopes", "adp/providers.Discover")
                                if component == "api" else
                                ("plugins/github/tasks.ExtractApiIssues", "plugins/jira/tasks.ExtractIssues"))
                    for symbol in required:
                        self.assertIn(symbol, symbols)
                    for asset in ("LICENSE", "NOTICE", "schema-manifest.json", "source-manifest.json", "module-contract.json"):
                        self.assertTrue((source / "backend" / "adp" / asset).is_file())
                    with tempfile.TemporaryDirectory(dir=temporary) as image_workspace:
                        layout = Path(temporary) / (component + "-image")
                        build.build_oci(lock, source, revision, component, layout, Path(image_workspace))
                        index = json.loads((layout / "index.json").read_text())
                        self.assertEqual(len(index["manifests"]), 1)
                        publisher_spec = importlib.util.spec_from_file_location(
                            "devlake_publish", ROOT / "modules/insights/poc/devlake/publish.py")
                        publisher = importlib.util.module_from_spec(publisher_spec)
                        publisher_spec.loader.exec_module(publisher)
                        schema_digest = hashlib.sha256(publisher.SCHEMA.read_bytes()).hexdigest()
                        self.assertEqual(publisher.inspect(layout, index["manifests"][0]["digest"],
                                                           revision, component), schema_digest)

                        def read_blob(descriptor):
                            content = (layout / "blobs" / "sha256" / descriptor["digest"][7:]).read_bytes()
                            self.assertEqual(hashlib.sha256(content).hexdigest(), descriptor["digest"][7:])
                            return content

                        manifest = json.loads(read_blob(index["manifests"][0]))
                        self.assertEqual(manifest["mediaType"], "application/vnd.oci.image.manifest.v1+json")
                        self.assertEqual(len(manifest["layers"]), 2)
                        for descriptor in manifest["layers"]:
                            read_blob(descriptor)
                        config = json.loads(read_blob(manifest["config"]))
                        self.assertEqual(config["architecture"], "amd64")
                        self.assertEqual(config["config"]["Entrypoint"], ["/usr/local/bin/" +
                                                                        ("connector-api" if component == "api" else "collector-worker")])
                        self.assertEqual(config["config"]["Labels"]["org.opencontainers.image.revision"], revision)
                        with tarfile.open(layout / "blobs" / "sha256" / manifest["layers"][-1]["digest"][7:], "r:*") as layer:
                            self.assertEqual(len(layer.getnames()), 6)
                            self.assertEqual(layer.extractfile("usr/share/devlake/schema-manifest.json").read(),
                                             (source / "backend/adp/schema-manifest.json").read_bytes())
                            self.assertEqual(layer.extractfile("usr/share/devlake/source-manifest.json").read(),
                                             (source / "backend/adp/source-manifest.json").read_bytes())
                    if component == "worker":
                        inventory = json.loads(subprocess.check_output([str(binary), "--verify-package"], text=True))
                        self.assertEqual(inventory["raw_collector"], "api.ApiCollector")
                        self.assertEqual(len(inventory["linked_extractors"]), 2)
                    else:
                        with socket.socket() as listener:
                            listener.bind(("127.0.0.1", 0))
                            port = listener.getsockname()[1]
                        process = subprocess.Popen([str(binary)], env={"PATH": "/usr/bin:/bin",
                                                   "CONNECTOR_API_LISTEN": f"127.0.0.1:{port}"},
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        try:
                            for attempt in range(30):
                                try:
                                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                                        status = json.load(response)
                                    break
                                except OSError:
                                    time.sleep(0.1)
                            else:
                                self.fail("connector API did not start without DSN")
                            self.assertEqual(status["status"], "packaged")
                            self.assertEqual(len(status["linked_provider_functions"]), 3)
                        finally:
                            process.terminate()
                            process.wait(timeout=3)
                raw = (source / "backend/helpers/pluginhelper/api/api_collector.go").read_text()
                graphql = (source / "backend/helpers/pluginhelper/api/graphql_collector.go").read_text()
                self.assertIn("raw table is not provisioned", raw)
                self.assertIn("raw table is not provisioned", graphql)
                self.assertNotIn("db.AutoMigrate", graphql)
                patch = ROOT / "modules/connectors/devlake/patches/0001-require-provisioned-raw-tables.patch"
                conflict = subprocess.run(["git", "-C", str(source), "apply", "--check", str(patch)],
                                          capture_output=True, text=True)
                self.assertNotEqual(conflict.returncode, 0)


class PublicationScenarioTest(unittest.TestCase):
    def test_publication(self):
        from modules.insights.tests.devlake_poc.test_s09_finalizer import FinalizerFixture

        fixture = FinalizerFixture("runTest")
        fixture.setUp()
        try:
            fixture.role = "publisher-role"
            with self.assertRaisesRegex(ValueError, "scheduled and manual"):
                fixture.finalize.preflight(fixture.uri, fixture.digest, "api")
            fixture.role = "finalizer-role"
            fixture.run_probes()
            fixture.role = "publisher-role"
            fixture.finalize.preflight(fixture.uri, fixture.digest, "api")
            with self.assertRaisesRegex(ValueError, "destination, digest"):
                fixture.finalize.preflight(fixture.uri, "sha256:" + "c" * 64, "api")
            self.assertEqual(fixture.record["artifacts"]["api"]["tag"], fixture.revision + "-api")
        finally:
            fixture.doCleanups()


class RecoveryScenarioTest(unittest.TestCase):
    def test_failed_build_retry(self):
        import datetime
        from modules.insights.tests.devlake_poc.test_s09_cleanup import CleanupFixture

        fixture = CleanupFixture("runTest")
        fixture.setUp()
        try:
            fixture.add_worker()
            frozen = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
            with mock.patch.object(fixture.artifacts, "now", return_value=frozen):
                fixture.artifacts.run("manual", "hold", "worker")
                fixture.denied = 1
                with self.assertRaisesRegex(ValueError, "cleanup authority unavailable"):
                    fixture.artifacts.run("manual", "failure")
                self.assertNotIn("receipt", fixture.record["artifacts"]["api"])
                fixture.artifacts.run("manual")
            self.assertEqual(fixture.record["finalization_requested"]["reason"], "failure")
            self.assertEqual(fixture.record["artifacts"]["api"]["cleanup"]["attempts"], 2)
            self.assertEqual(fixture.record["artifacts"]["api"]["receipt"]["outcome"], "absent")
            self.assertEqual(fixture.record["artifacts"]["worker"]["receipt"]["outcome"], "retained")
            self.assertEqual(fixture.record["artifacts"]["api"]["receipt"]["verified_at"], frozen.isoformat())
            self.assertEqual(fixture.images["api"]["previous-good"], "sha256:" + "d" * 64)
            self.assertIn("finalization_complete", fixture.record)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
