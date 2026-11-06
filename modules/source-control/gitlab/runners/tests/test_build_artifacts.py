import hashlib
import importlib.util
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_artifacts.py"
spec = importlib.util.spec_from_file_location("build_artifacts", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "templates").mkdir()
        (self.root / "templates/job-user-data.sh.tftpl").write_bytes(b"reviewed synthetic bootstrap")
        (self.root / "bootstrap").mkdir()
        (self.root / "bootstrap/requirements.lock").write_bytes(b"reviewed synthetic dependency lock")
        self.plugin = b"reviewed synthetic fleeting archive"
        self.lock = {
            "bootstrap_python_image": "python:3.12.8-slim@sha256:" + "a" * 64,
            "bootstrap_requirements_sha256": hashlib.sha256(b"reviewed synthetic dependency lock").hexdigest(),
            "host_ami": {"us-east-1": {"image_id": "ami-12345678", "owner_id": "123456789012",
                                       "architecture": "x86_64", "bootstrap_sha256": hashlib.sha256(b"reviewed synthetic bootstrap").hexdigest()}},
            "fleeting_plugin": {"s3_key": "gitlab-runners/upstream/fleeting-1.0.0.tar.gz",
                                "sha256": hashlib.sha256(self.plugin).hexdigest()},
        }
        self.lock_path = self.root / "versions.lock.json"
        self.save_lock()

    def save_lock(self):
        self.lock_path.write_text(json.dumps(self.lock))

    def test_missing_and_unpinned_release_inputs_fail(self):
        for change in ({"host_ami": {}}, {"fleeting_plugin": {"s3_key": "gitlab-runners/upstream/latest", "sha256": hashlib.sha256(self.plugin).hexdigest()}},
                       {"fleeting_plugin": {"s3_key": "unrelated/plugin", "sha256": "a" * 64}}):
            with self.subTest(change=change), self.assertRaises(builder.BuildError):
                builder.release_inputs({**self.lock, **change}, "us-east-1", "private-test-bucket")
        with self.assertRaises(builder.BuildError):
            builder.build(self.root / "missing.json", "us-east-1", "private-test-bucket", "a" * 40)

    def test_bootstrap_image_and_dependencies_must_be_immutable(self):
        with patch.object(builder, "ROOT", self.root):
            self.assertEqual(builder.bootstrap_base(self.lock), self.lock["bootstrap_python_image"])
            self.lock["bootstrap_python_image"] = "python:latest"
            with self.assertRaises(builder.BuildError):
                builder.bootstrap_base(self.lock)
            self.lock["bootstrap_python_image"] = "python:3.12.8-slim@sha256:" + "a" * 64
            (self.root / "bootstrap/requirements.lock").write_text("changed")
            with self.assertRaises(builder.BuildError):
                builder.bootstrap_base(self.lock)

    def test_ami_must_match_owner_architecture_state_and_visibility(self):
        image = {"ImageId": "ami-12345678", "OwnerId": "123456789012", "Architecture": "x86_64",
                 "State": "available", "Public": False}
        with patch.object(builder, "checked_aws", return_value=json.dumps({"Images": [image]}).encode()):
            builder.verify_ami(self.lock["host_ami"]["us-east-1"], "us-east-1")
        for change in ({"OwnerId": "000000000000"}, {"Architecture": "arm64"},
                       {"State": "pending"}, {"Public": True}):
            with self.subTest(change=change), patch.object(builder, "checked_aws", return_value=json.dumps({"Images": [{**image, **change}]}).encode()), self.assertRaises(builder.BuildError):
                builder.verify_ami(self.lock["host_ami"]["us-east-1"], "us-east-1")

    def test_publishes_only_verified_bootstrap_and_plugin_with_source_manifest(self):
        image = {"ImageId": "ami-12345678", "OwnerId": "123456789012", "Architecture": "x86_64",
                 "State": "available", "Public": False}
        uploaded = {}

        def fake_aws(*args):
            if args[:2] == ("ec2", "describe-images"):
                return json.dumps({"Images": [image]}).encode()
            if args[:2] == ("s3", "cp") and args[3] == "-":
                return self.plugin
            if args[:2] == ("s3", "cp"):
                with tarfile.open(args[2], "r:gz") as archive:
                    uploaded.update({member.name: archive.extractfile(member).read() for member in archive.getmembers()})
                self.assertIn("gitlab-runners/artifacts/" + "a" * 40, args[3])
                return b""
            self.fail("unexpected AWS operation")

        with patch.object(builder, "ROOT", self.root), patch.object(builder, "checked_aws", side_effect=fake_aws):
            builder.build(self.lock_path, "us-east-1", "private-test-bucket", "a" * 40)
        self.assertEqual(uploaded["fleeting-plugin"], self.plugin)
        self.assertEqual(uploaded["job-user-data.sh.tftpl"], b"reviewed synthetic bootstrap")
        self.assertEqual(json.loads(uploaded["manifest.json"])["source_sha"], "a" * 40)

    def test_wrong_bootstrap_or_plugin_hash_prevents_publication(self):
        self.lock["host_ami"]["us-east-1"]["bootstrap_sha256"] = "a" * 64
        self.save_lock()
        with patch.object(builder, "ROOT", self.root), patch.object(builder, "checked_aws") as aws, self.assertRaises(builder.BuildError):
            builder.build(self.lock_path, "us-east-1", "private-test-bucket", "a" * 40)
        aws.assert_not_called()
        self.lock["host_ami"]["us-east-1"]["bootstrap_sha256"] = hashlib.sha256(b"reviewed synthetic bootstrap").hexdigest()
        self.lock["fleeting_plugin"]["sha256"] = "b" * 64
        self.save_lock()
        image = {"ImageId": "ami-12345678", "OwnerId": "123456789012", "Architecture": "x86_64",
                 "State": "available", "Public": False}
        with patch.object(builder, "ROOT", self.root), patch.object(builder, "checked_aws", side_effect=[json.dumps({"Images": [image]}).encode(), self.plugin]) as aws, self.assertRaises(builder.BuildError):
            builder.build(self.lock_path, "us-east-1", "private-test-bucket", "a" * 40)
        self.assertEqual(aws.call_count, 2)

    def test_connector_metadata_and_checksum_refuse_before_staging(self):
        plugin = {"version": "v1.3.1", "source_commit": "bf5cbcc9183e164637cb4dae42e64e49365e8ade",
                  "s3_key": "gitlab-runners/upstream/fleeting-plugin-aws-linux-amd64",
                  "sha256": "2090afe6b2a888d1e58990dbf7b731124956d27358ff8b8532180235d9d49ebb"}
        output = self.root / "fleeting-plugin-aws"
        for key in plugin:
            with self.subTest(key=key), patch.object(builder, "checked_aws") as aws, self.assertRaises(builder.BuildError):
                builder.prepare_plugin({"fleeting_plugin": {**plugin, key: "substituted"}},
                                       "us-east-1", "private-test-bucket", output)
            aws.assert_not_called()
        with patch.object(builder, "checked_aws", return_value=b"wrong artifact") as aws, self.assertRaises(builder.BuildError):
            builder.prepare_plugin({"fleeting_plugin": plugin}, "us-east-1", "private-test-bucket", output)
        self.assertFalse(output.exists())
        self.assertEqual(aws.call_count, 1)

    def test_staged_connector_is_executable_and_cannot_overwrite_context_files(self):
        raw = bytearray(64)
        raw[:7] = b"\x7fELF\x02\x01\x01"
        raw[18:20] = (62).to_bytes(2, "little")
        output = self.root / "fleeting-plugin-aws"
        builder.stage_plugin(raw, hashlib.sha256(raw).hexdigest(), output)
        self.assertEqual(output.read_bytes(), raw)
        self.assertEqual(output.stat().st_mode & 0o777, 0o555)
        with self.assertRaises(builder.BuildError):
            builder.stage_plugin(raw, hashlib.sha256(raw).hexdigest(), output)
        for bad in (b"not an executable", bytes(raw[:18] + b"\xb7\x00" + raw[20:])):
            with self.subTest(artifact=bad), self.assertRaises(builder.BuildError):
                builder.stage_plugin(bad, hashlib.sha256(bad).hexdigest(), self.root / "invalid-plugin")
        self.assertFalse((self.root / "invalid-plugin").exists())


if __name__ == "__main__":
    unittest.main()


class BuildRecipeTests(unittest.TestCase):
    def test_codebuild_recipe_and_image_do_not_bake_credentials_or_mutable_tags(self):
        root = Path(__file__).resolve().parents[5]
        recipe = (root / "codebuild/bs-gitlab-runner-bootstrap.yml").read_text()
        publisher = (root / "modules/source-control/gitlab/runners/scripts/build_runtime_images.py").read_text()
        dockerfile = (root / "modules/source-control/gitlab/runners/bootstrap/Dockerfile").read_text()
        for required in ("ADP_SOURCE_SHA", "EXPECTED_ACCOUNT", "build_runtime_images.py --receipt"):
            self.assertIn(required, recipe)
        for required in ("prepare_plugin", "bootstrap_base", '"--pull", "--no-cache"', "imageDigest", "IMMUTABLE"):
            self.assertIn(required, publisher)
        self.assertIn("--require-hashes", dockerfile)
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn("COPY --chmod=0555 fleeting-plugin-aws /opt/fleeting/fleeting-plugin-aws", dockerfile)
        self.assertNotIn("build_artifacts.py build\n", recipe)
        for forbidden in ("latest", "glrt-", "SECRET_ACCESS_KEY", "runner-token", "--build-arg TOKEN"):
            self.assertNotIn(forbidden, recipe + dockerfile)
