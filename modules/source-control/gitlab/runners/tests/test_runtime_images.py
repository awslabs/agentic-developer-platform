"""Publication must follow actual runtime checks and retain partial results."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_runtime_images as builder
import check_job_images as checker


class CommittedBuildInputsTests(unittest.TestCase):
    def test_checked_in_release_can_start_image_build_before_ami_production(self):
        lock = json.loads((builder.ROOT / "versions.lock.json").read_text())
        # Validate the real committed inputs; synthetic publication fixtures can
        # otherwise hide a missing release pin until the first protected build.
        with patch.object(builder, "command") as command:
            self.assertEqual(builder.inputs(lock, builder.ROOT), lock["bootstrap_python_image"])
        command.assert_not_called()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.images = {name: f"example/{name}:reviewed" for name in ("bootstrap", "helper", "job")}
        self.lock = {"fleeting_plugin": {"sha256": "a" * 64}}
        self.receipt = {"images": {}, "status": "building"}

    def test_no_image_published_when_any_runtime_probe_fails(self):
        for target in ("bootstrap", "job"):
            with self.subTest(target=target), patch.object(builder.check_bootstrap_image, "check") as bootstrap, \
                    patch.object(builder.check_job_images, "check") as job:
                (bootstrap if target == "bootstrap" else job).side_effect = ValueError("runtime failure")
                published = []
                with self.assertRaises(ValueError):
                    builder.qualify_and_publish(self.images, self.lock, lambda *a: published.append(a), self.receipt, lambda: None)
                self.assertEqual(published, [])

    def test_partial_publication_is_retained_and_never_reports_complete(self):
        saved = []
        published = []

        def publish(name, image):
            published.append(name)
            if name == "helper":
                raise ValueError("provider unavailable")
            return image + "@sha256:" + "b" * 64

        with patch.object(builder.check_bootstrap_image, "check"), \
                patch.object(builder.check_job_images, "check", return_value={}), self.assertRaises(ValueError):
            builder.qualify_and_publish(self.images, self.lock, publish, self.receipt,
                                        lambda: saved.append(copy.deepcopy(self.receipt)))
        self.assertEqual(published, ["bootstrap", "helper"])
        self.assertEqual(list(saved[-1]["images"]), ["bootstrap"])
        self.assertNotEqual(saved[-1]["status"], "images_published")

    def test_complete_receipt_binds_probe_to_qualified_job_without_ami_claim(self):
        with patch.object(builder.check_bootstrap_image, "check"), \
                patch.object(builder.check_job_images, "check", return_value={}):
            builder.qualify_and_publish(self.images, self.lock, lambda name, image: image + "@sha256:" + "b" * 64,
                                        self.receipt, lambda: None)
        self.assertEqual(self.receipt["probe_image"], self.receipt["images"]["job"])
        self.assertEqual(self.receipt["status"], "images_published")
        self.assertFalse(self.receipt["ami_produced"])
        self.assertFalse(self.receipt["live_qualified"])

    def test_missing_or_changed_source_refuses_before_external_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for revision in ("main", "a" * 40):
                with self.subTest(revision=revision), patch.object(builder, "command") as command, self.assertRaises((ValueError, OSError)):
                    builder.build(revision, "us-east-1", "123456789012", "private-test-bucket", root / "receipt.json", root=root)
                command.assert_not_called()

    def test_existing_receipt_refuses_retry_before_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            receipt.write_text('{"status":"failed_reconcile_before_retry"}')
            with patch.object(builder, "command") as command, self.assertRaises(ValueError):
                builder.build("a" * 40, "us-east-1", "123456789012", "private-test-bucket", receipt)
            command.assert_not_called()
            self.assertEqual(json.loads(receipt.read_text())["status"], "failed_reconcile_before_retry")

    def test_receipt_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            builder.save_receipt(path, {"status": "building"})
            builder.save_receipt(path, {"status": "failed"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"status": "failed"})
            self.assertFalse(path.with_suffix(".tmp").exists())


class BuildOrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_path = Path(temporary.name) / "receipt.json"
        self.uploads = []
        self.objects = {}
        self.pushes = []
        self.push_error = RuntimeError("synthetic private provider failure")
        self.upload_error = RuntimeError("synthetic private upload failure")

    def build(self, *, fail_push=False, fail_upload=False, fail_canonical=False, fail_failure_upload=False):
        registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"

        def command(args, **kwargs):
            if args[:3] == ["aws", "sts", "get-caller-identity"]:
                return b'{"Account":"123456789012"}'
            if args[:3] == ["aws", "ecr", "describe-repositories"]:
                repository = args[args.index("--repository-names") + 1]
                return json.dumps({"repositories": [{"registryId": "123456789012",
                    "repositoryUri": f"{registry}/{repository}", "imageTagMutability": "IMMUTABLE"}]}).encode()
            if args[:3] == ["aws", "ecr", "batch-get-image"]:
                return b'{"failures":[{"failureCode":"ImageNotFound"}]}'
            if args[:3] == ["aws", "ecr", "describe-images"]:
                return json.dumps({"imageDetails": [{"imageTags": ["a" * 40],
                    "imageDigest": "sha256:" + "b" * 64}]}).encode()
            if args[:3] == ["aws", "ecr", "get-login-password"]:
                return b"synthetic-registry-password"
            if args[0] == "docker":
                if "push" in args:
                    self.pushes.append(args[-1])
                    if fail_push and "-helper:" in args[-1]:
                        raise self.push_error
                return b""
            if args[:3] == ["aws", "s3", "cp"]:
                key, source = args[4], Path(args[3])
                document = json.loads(source.read_text())
                self.uploads.append((key, document))
                if ((fail_upload and document["images"])
                        or (fail_failure_upload and document["status"] == "failed_reconcile_before_retry")):
                    raise self.upload_error
                self.objects[key] = document
                return b""
            if args[:3] == ["aws", "s3api", "put-object"]:
                self.assertEqual(args[args.index("--if-none-match") + 1], "*")
                key = "s3://" + args[args.index("--bucket") + 1] + "/" + args[args.index("--key") + 1]
                if fail_canonical or key in self.objects:
                    raise self.upload_error
                self.objects[key] = json.loads(Path(args[args.index("--body") + 1]).read_text())
                return b""
            self.fail(f"unexpected command: {args[:3]}")

        with patch.object(builder, "inputs", return_value="reviewed-base"), \
                patch.object(builder.build_artifacts, "prepare_plugin"), \
                patch.object(builder.check_bootstrap_image, "check"), \
                patch.object(builder.check_job_images, "check", return_value={}), \
                patch.object(builder, "command", side_effect=command):
            return builder.build("a" * 40, "us-east-1", "123456789012", "private-test-bucket", self.receipt_path)

    def test_partial_publication_is_retained_remotely_by_the_build(self):
        with self.assertRaises(RuntimeError) as raised:
            self.build(fail_push=True)
        self.assertIs(raised.exception, self.push_error)
        self.assertTrue(self.uploads)
        key, receipt = self.uploads[-1]
        self.assertIn("/attempts/", key)
        self.assertEqual(receipt["status"], "failed_reconcile_before_retry")
        self.assertEqual(list(receipt["images"]), ["bootstrap"])
        self.assertEqual(self.objects[key], receipt)

    def test_failed_receipt_upload_stops_publication_and_preserves_original_error(self):
        with self.assertRaises(RuntimeError) as raised:
            self.build(fail_upload=True)
        self.assertIs(raised.exception, self.upload_error)
        self.assertEqual(len(self.pushes), 1)
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(list(receipt["images"]), ["bootstrap"])
        self.assertEqual(receipt["status"], "failed_reconcile_before_retry")

    def test_failure_receipt_upload_never_masks_the_provider_failure(self):
        with self.assertRaises(RuntimeError) as raised:
            self.build(fail_push=True, fail_failure_upload=True)
        self.assertIs(raised.exception, self.push_error)
        self.assertTrue(any(list(document["images"]) == ["bootstrap"] for document in self.objects.values()))
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt["status"], "failed_reconcile_before_retry")

    def test_failure_receipt_does_not_overwrite_success_and_attempts_are_distinct(self):
        successful = self.build()
        canonical = next(key for key in self.objects if "/attempts/" not in key)
        self.receipt_path = self.receipt_path.with_name("retry.json")
        with self.assertRaises(RuntimeError):
            self.build(fail_push=True)
        self.assertEqual(self.objects[canonical], successful)
        attempts = {key for key, _ in self.uploads if "/attempts/" in key}
        self.assertEqual(len(attempts), 2)

    def test_canonical_receipt_cannot_overwrite_a_prior_success(self):
        successful = self.build()
        canonical = next(key for key in self.objects if "/attempts/" not in key)
        self.receipt_path = self.receipt_path.with_name("retry.json")
        with self.assertRaises(RuntimeError) as raised:
            self.build()
        self.assertIs(raised.exception, self.upload_error)
        self.assertEqual(self.objects[canonical], successful)
        self.assertEqual(self.uploads[-1][1]["status"], "failed_reconcile_before_retry")

    def test_canonical_upload_failure_retains_attempt_and_preserves_error(self):
        with self.assertRaises(RuntimeError) as raised:
            self.build(fail_canonical=True)
        self.assertIs(raised.exception, self.upload_error)
        key, receipt = self.uploads[-1]
        self.assertEqual(set(receipt["images"]), {"bootstrap", "helper", "job"})
        self.assertEqual(receipt["status"], "failed_reconcile_before_retry")
        self.assertEqual(self.objects[key], receipt)


class JobRuntimeTests(unittest.TestCase):
    def fake_docker(self, failure=None, user="10001:10001", changed_owner=False):
        calls, volumes = [], {}

        def run(args, **kwargs):
            calls.append(args)
            stdout, code = b"", 0
            if args[1:3] == ["image", "inspect"]:
                stdout = json.dumps([{"Os": "linux", "Architecture": "amd64", "Config": {"User": user}}]).encode()
            elif args[1:3] == ["volume", "create"]:
                volumes[args[-1]] = args[-2].split("=", 1)[1]
            elif args[1:3] == ["volume", "inspect"]:
                stdout = json.dumps([{"Name": args[-1], "Labels": {"adp-image-check": "foreign" if changed_owner else volumes[args[-1]]}}]).encode()
            elif args[1] == "run" and failure in ("helper", "job") and f"example/{failure}:reviewed" in args:
                code = 1
            return subprocess.CompletedProcess(args, code, stdout, b"private error")

        return run, calls

    def test_real_command_path_checks_helper_then_job_without_identity_override(self):
        run, calls = self.fake_docker()
        checker.check("example/job:reviewed", "example/helper:reviewed", run=run)
        containers = [args for args in calls if args[1] == "run"]
        self.assertEqual(len(containers), 2)
        self.assertIn("cache-init", containers[0])
        self.assertIn("python3", containers[1])
        for args in containers:
            self.assertIn("--network=none", args)
            self.assertIn("--cap-drop=ALL", args)
            self.assertIn("--security-opt=no-new-privileges:true", args)
            self.assertNotIn("--user", args)

    def test_root_default_fails_before_volume_or_container_creation(self):
        run, calls = self.fake_docker(user="root")
        with self.assertRaises(checker.ImageCheckError):
            checker.check("example/job:reviewed", "example/helper:reviewed", run=run)
        self.assertEqual(len(calls), 1)

    def test_failed_helper_and_job_remove_only_owned_test_volumes(self):
        for failure in ("helper", "job"):
            run, calls = self.fake_docker(failure=failure)
            with self.subTest(failure=failure), self.assertRaises(checker.ImageCheckError):
                checker.check("example/job:reviewed", "example/helper:reviewed", run=run)
            removed = [args[-1] for args in calls if args[1:3] == ["volume", "rm"]]
            created = [args[-1] for args in calls if args[1:3] == ["volume", "create"]]
            self.assertEqual(removed, created)
            self.assertNotIn("prune", str(calls))

    def test_changed_volume_ownership_refuses_deletion(self):
        run, calls = self.fake_docker(changed_owner=True)
        with self.assertRaises(checker.ImageCheckError):
            checker.check("example/job:reviewed", "example/helper:reviewed", run=run)
        self.assertFalse(any(args[1:3] == ["volume", "rm"] for args in calls))


if __name__ == "__main__":
    unittest.main()
