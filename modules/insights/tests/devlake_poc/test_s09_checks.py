import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake/scripts/checks.py"


class CheckedEvidenceTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("devlake_checks", MODULE)
        self.checks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checks)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.digest = "sha256:" + "a" * 64
        self.account = "123456789012"
        self.uri = f"{self.account}.dkr.ecr.us-east-1.amazonaws.com/devlake-poc/api"
        self.args = SimpleNamespace(revision=self.revision, component="api", expected_digest=self.digest,
                                    layout=Path(self.temporary.name) / "image", account=self.account,
                                    region="us-east-1", repository_uri=self.uri,
                                    output=Path(self.temporary.name) / "checks.json")
        self.sbom = {"bomFormat": "CycloneDX", "components": [{"name": "example-module"}]}

    def scanner_output(self, command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return self.revision
        if command == ["syft", "version", "-o", "json"]:
            return json.dumps({"version": "1.42.0"})
        if command[:2] == ["syft", "scan"]:
            return json.dumps(self.sbom)
        if command == ["gitleaks", "version"]:
            return "v8.30.0\n"
        self.fail(f"unexpected command {command}")

    def test_complete_checks_bind_digest_and_sbom_before_finalization(self):
        events = []

        def run(*command, **kwargs):
            events.append(command)

        def preflight(*scope):
            self.assertEqual(scope, (self.uri, self.digest, "api"))
            events.append("finalizer")

        with mock.patch("subprocess.check_output", side_effect=self.scanner_output), \
                mock.patch.object(self.checks, "run", side_effect=run), \
                mock.patch.object(self.checks.publish, "inspect", return_value="b" * 64), \
                mock.patch.object(self.checks, "scan_layers") as scan:
            sbom, evidence = self.checks.produce(self.args, preflight)
        self.assertEqual(events[0][:3], (str(self.checks.BUILD), "--customization", self.revision))
        self.assertIn("--destination", events[1])
        self.assertEqual(events[2][:3], ("go", "test", "-mod=readonly"))
        self.assertEqual(events[3][:2], ("gitleaks", "dir"))
        self.assertEqual(events[4], "finalizer")
        scan.assert_called_once()
        self.assertEqual(evidence["image_digest"], self.digest)
        self.assertEqual(evidence["schema_sha256"], "b" * 64)
        self.assertEqual(set(evidence["checks"]), self.checks.publish.REQUIRED_CHECKS)
        self.assertEqual(set(evidence["checks"].values()), {"passed"})
        self.args.output.with_name("sbom.json").write_bytes(sbom)
        self.args.output.write_text(json.dumps(evidence))
        self.assertEqual(self.checks.publish.check_evidence(self.args.output, self.revision, "api",
                                                            self.digest, "b" * 64), evidence["sbom_sha256"])
        self.args.output.with_name("sbom.json").write_text("changed")
        with self.assertRaisesRegex(ValueError, "SBOM differs"):
            self.checks.publish.check_evidence(self.args.output, self.revision, "api", self.digest, "b" * 64)

    def test_entrypoint_writes_private_evidence_only_after_checks_succeed(self):
        arguments = ["checks.py", "--layout", str(self.args.layout), "--component", "api",
                     "--expected-digest", self.digest, "--revision", self.revision,
                     "--account", self.account, "--region", self.args.region,
                     "--repository-uri", self.uri, "--output", str(self.args.output)]
        finalizer = SimpleNamespace(preflight=lambda *args: None)
        with mock.patch.object(sys, "argv", arguments), mock.patch.dict(sys.modules, {"finalize": finalizer}), \
                mock.patch.object(self.checks, "produce", return_value=(b'{}\n', {"image_digest": self.digest})):
            self.checks.main()
        self.assertEqual(json.loads(self.args.output.read_text())["image_digest"], self.digest)
        self.assertEqual(self.args.output.with_name("sbom.json").read_bytes(), b'{}\n')
        self.assertEqual(self.args.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.args.output.with_name("sbom.json").stat().st_mode & 0o777, 0o600)
        self.args.output.unlink()
        self.args.output.with_name("sbom.json").unlink()
        with mock.patch.object(sys, "argv", arguments), mock.patch.dict(sys.modules, {"finalize": finalizer}), \
                mock.patch.object(self.checks, "produce", side_effect=ValueError("failed check")):
            with self.assertRaisesRegex(ValueError, "failed check"):
                self.checks.main()
        self.assertFalse(self.args.output.exists())
        self.assertFalse(self.args.output.with_name("sbom.json").exists())

    def test_failed_scans_and_preflight_produce_no_evidence(self):
        def fail_secrets(*command, **kwargs):
            if command[0] == "gitleaks":
                raise subprocess.CalledProcessError(1, command)

        with mock.patch("subprocess.check_output", side_effect=self.scanner_output), \
                mock.patch.object(self.checks.publish, "inspect", return_value="b" * 64), \
                mock.patch.object(self.checks, "scan_layers"):
            for failure in ("secrets", "finalizer"):
                with self.subTest(failure=failure), mock.patch.object(self.checks, "run", side_effect=fail_secrets):
                    finalizer = (lambda *args: (_ for _ in ()).throw(ValueError("unprotected ledger"))
                                 if failure == "finalizer" else lambda *args: self.fail("finalizer ran after failed scan"))
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        self.checks.produce(self.args, finalizer)
                    self.assertFalse(self.args.output.exists())
                    self.assertFalse(self.args.output.with_name("sbom.json").exists())

    def test_missing_scanner_or_wrong_image_refuses_before_finalizer(self):
        with mock.patch.object(self.checks.publish, "inspect", side_effect=ValueError("wrong image")), \
                mock.patch.object(self.checks, "run") as run:
            with self.assertRaisesRegex(ValueError, "wrong image"):
                self.checks.produce(self.args, lambda *args: self.fail("finalizer should not run"))
            self.assertEqual(run.call_count, 1)
        def missing_scanner(command, **kwargs):
            if command == ["syft", "version", "-o", "json"]:
                raise FileNotFoundError("syft missing")
            return self.scanner_output(command, **kwargs)

        with mock.patch("subprocess.check_output", side_effect=missing_scanner), \
                mock.patch.object(self.checks.publish, "inspect", return_value="b" * 64), \
                mock.patch.object(self.checks, "run"):
            with self.assertRaises(FileNotFoundError):
                self.checks.produce(self.args, lambda *args: self.fail("finalizer should not run"))
        self.assertFalse(self.args.output.exists())

    def test_wrong_destination_or_scanner_version_refuses_evidence(self):
        self.args.repository_uri = self.uri.replace("devlake-poc/api", "devlake-poc/worker")
        with self.assertRaisesRegex(ValueError, "exact scoped ECR"):
            self.checks.produce(self.args, lambda *args: self.fail("unexpected finalizer"))
        self.args.repository_uri = self.uri

        def wrong_version(command, **kwargs):
            if command == ["syft", "version", "-o", "json"]:
                return json.dumps({"version": "1.52.0"})
            return self.scanner_output(command, **kwargs)

        with mock.patch("subprocess.check_output", side_effect=wrong_version), \
                mock.patch.object(self.checks.publish, "inspect", return_value="b" * 64), \
                mock.patch.object(self.checks, "run"):
            with self.assertRaisesRegex(ValueError, "SBOM scanner version"):
                self.checks.produce(self.args, lambda *args: self.fail("unexpected finalizer"))
        self.assertFalse(self.args.output.exists())

    def test_reject_traversal_in_layer_before_secret_scan(self):
        self.args.layout.mkdir()
        (self.args.layout / "index.json").write_text(json.dumps({"manifests": [{"digest": "manifest"}]}))
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as layer:
            member = tarfile.TarInfo("../outside")
            member.size = 6
            layer.addfile(member, io.BytesIO(b"secret"))
        def blob(layout, descriptor):
            if descriptor["digest"] == "manifest":
                return json.dumps({"layers": [{"digest": "layer"}]}).encode()
            return archive.getvalue()
        with mock.patch.object(self.checks.publish, "blob", side_effect=blob):
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                self.checks.scan_layers(self.args.layout, Path(self.temporary.name) / "extracted")
        self.assertFalse((Path(self.temporary.name) / "outside").exists())


if __name__ == "__main__":
    unittest.main()
