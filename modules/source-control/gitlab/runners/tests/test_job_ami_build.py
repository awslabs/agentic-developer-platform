"""A candidate image must match reviewed source, target and independent AWS metadata."""
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import uuid
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_job_ami.py"
spec = importlib.util.spec_from_file_location("job_ami_builder", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AmiBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for path in ("bootstrap", "templates"):
            (self.root / path).mkdir()
        (self.root / "bootstrap/verify_images.py").write_text("synthetic image verifier")
        (self.root / "templates/job-user-data.sh.tftpl").write_text("synthetic host guard")
        self.verifier_hash = hashlib.sha256((self.root / "bootstrap/verify_images.py").read_bytes()).hexdigest()
        (self.root / "versions.lock.json").write_text(json.dumps({"image_verifier_sha256": self.verifier_hash}))
        self.config = {"account_id": "123456789012", "region": "us-east-1", "vpc_id": "vpc-00000000",
                       "subnet_id": "subnet-00000000", "security_group_id": "sg-00000000", "source_ami": "ami-00000000",
                       "cluster_name": "test-cluster", "source_revision": "a" * 40,
                       "operation_id": "00000000-0000-0000-0000-000000000001",
                       "instance_profile": "adp-dev-gitlab-runner-image-builder",
                       "expected_role_arn": "arn:aws:iam::123456789012:role/reviewed-builder"}
        self.images = {"status": "images_published", "local_only": False, "source_revision": "a" * 40,
                       "account_id": "123456789012", "region": "us-east-1", "images": {},
                       "runtime_checks": {"actual_helper_cache_init": "pass", "job_volume_writes": "pass",
                                          "bootstrap_both_uids_and_connector": "pass", "uid": 10001,
                                          "capabilities": "none", "no_new_privileges": True}}
        for name in ("bootstrap", "job", "helper"):
            self.images["images"][name] = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gitlab-runner-{name}:" + "a" * 40 + "@sha256:" + "b" * 64

    def target_responses(self):
        return {
            ("sts", "get-caller-identity"): {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/reviewed-builder/build"},
            ("eks", "describe-cluster"): {"cluster": {"status": "ACTIVE", "resourcesVpcConfig": {"vpcId": "vpc-00000000"}}},
            ("ec2", "describe-subnets"): {"Subnets": [{"VpcId": "vpc-00000000", "OwnerId": "123456789012", "MapPublicIpOnLaunch": False}]},
            ("ec2", "describe-security-groups"): {"SecurityGroups": [{"VpcId": "vpc-00000000", "OwnerId": "123456789012", "IpPermissions": []}]},
            ("iam", "get-instance-profile"): {"InstanceProfile": {"Arn": "arn:aws:iam::123456789012:instance-profile/adp-dev-gitlab-runner-image-builder", "Roles": [{"Arn": "arn:aws:iam::123456789012:role/adp-dev-gitlab-runner-image-builder"}]}},
            ("ec2", "describe-images"): {"Images": [{"ImageId": "ami-00000000", "OwnerId": "137112412989", "Architecture": "x86_64", "State": "available", "RootDeviceName": "/dev/xvda", "Name": "al2023-ami-2023.synthetic-test"}]},
            ("ec2", "describe-instances"): {"Reservations": []},
            ("ec2", "describe-key-pairs"): {"KeyPairs": []},
        }

    def test_incomplete_images_wrong_source_or_local_receipt_refuses(self):
        for change in ({"status": "failed"}, {"local_only": True}, {"source_revision": "c" * 40},
                       {"account_id": "000000000000"}, {"images": {}}, {"runtime_checks": {}}):
            with self.subTest(change=change), self.assertRaises(builder.AmiBuildError):
                builder.source_inputs(self.config, {**self.images, **change}, self.root)

    def test_substituted_image_and_changed_verifier_refuse(self):
        wrong = copy.deepcopy(self.images)
        wrong["images"]["job"] = "python:latest"
        with self.assertRaises(builder.AmiBuildError):
            builder.source_inputs(self.config, wrong, self.root)
        (self.root / "bootstrap/verify_images.py").write_text("changed verifier")
        with self.assertRaises(builder.AmiBuildError):
            builder.source_inputs(self.config, self.images, self.root)

    def test_builder_profile_and_shell_interpolation_are_not_freeform(self):
        for field, value in (("instance_profile", "platform-admin"), ("region", "us-east-1;false"),
                             ("operation_id", "$(id)"), ("source_revision", "main")):
            with self.subTest(field=field), self.assertRaises(builder.AmiBuildError):
                builder.source_inputs({**self.config, field: value}, self.images, self.root)

    def test_hashes_bind_actual_source_and_both_digests(self):
        values = builder.source_inputs(self.config, self.images, self.root)
        expected = hashlib.sha256((self.images["images"]["job"] + "\n" + self.images["images"]["helper"] + "\n").encode()).hexdigest()
        self.assertEqual(values["images_sha256"], expected)
        self.assertEqual(values["bootstrap_sha256"], hashlib.sha256(b"synthetic host guard").hexdigest())

    def test_target_preflight_uses_only_reads(self):
        responses, calls = self.target_responses(), []

        def call(*args):
            calls.append(args)
            return {"Images": []} if args[:2] == ("ec2", "describe-images") and "--filters" in args else responses[args[:2]]

        builder.verify_target(self.config, call)
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(args[1].startswith(("get-", "describe-")) for args in calls))

    def test_wrong_role_public_subnet_other_vpc_ingress_profile_and_base_refuse(self):
        cases = [
            (("sts", "get-caller-identity"), {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/foreign/session"}),
            (("eks", "describe-cluster"), {"cluster": {"status": "ACTIVE", "resourcesVpcConfig": {"vpcId": "vpc-11111111"}}}),
            (("ec2", "describe-subnets"), {"Subnets": [{"VpcId": "vpc-00000000", "OwnerId": "123456789012", "MapPublicIpOnLaunch": True}]}),
            (("ec2", "describe-security-groups"), {"SecurityGroups": [{"VpcId": "vpc-00000000", "OwnerId": "123456789012", "IpPermissions": [{"IpProtocol": "-1"}]}]}),
            (("iam", "get-instance-profile"), {"InstanceProfile": {"Arn": "arn:aws:iam::123456789012:instance-profile/adp-dev-gitlab-runner-image-builder", "Roles": [{"Arn": "arn:aws:iam::123456789012:role/admin"}]}}),
            (("ec2", "describe-images"), {"Images": []}),
        ]
        for key, bad in cases:
            responses = self.target_responses()
            responses[key] = bad
            with self.subTest(key=key), self.assertRaises(builder.AmiBuildError):
                builder.verify_target(self.config, lambda *args: {"Images": []} if "--filters" in args else responses[args[:2]])

    def test_existing_operation_image_requires_reconciliation(self):
        responses = self.target_responses()
        with self.assertRaises(builder.AmiBuildError):
            builder.verify_target(self.config, lambda *args: {"Images": [{"ImageId": "ami-00000001"}]} if "--filters" in args else responses[args[:2]])

    def test_existing_builder_or_key_refuses_before_another_launch(self):
        for key, response in [
            (("ec2", "describe-instances"), {"Reservations": [{"Instances": [{"InstanceId": "i-00000001"}]}]}),
            (("ec2", "describe-key-pairs"), {"KeyPairs": [{"KeyName": "owned-operation-key"}]}),
            (("ec2", "describe-instances"), {}),
        ]:
            responses = self.target_responses()
            responses[key] = response
            calls = []

            def call(*args):
                calls.append(args)
                return {"Images": []} if args[:2] == ("ec2", "describe-images") and "--filters" in args else responses[args[:2]]

            with self.subTest(key=key), self.assertRaises(builder.AmiBuildError):
                builder.verify_target(self.config, call)
            self.assertTrue(all(args[1].startswith(("get-", "describe-")) for args in calls))

    def test_published_receipt_is_compared_with_immutable_registry(self):
        def call(*args):
            if args[1] == "describe-repositories":
                return {"repositories": [{"imageTagMutability": "IMMUTABLE", "registryId": "123456789012"}]}
            return {"imageDetails": [{"imageTags": ["a" * 40], "imageDigest": "sha256:" + "b" * 64}]}

        builder.verify_published_images(self.config, self.images, call)
        for operation, response in [
            ("describe-repositories", {"repositories": [{"imageTagMutability": "MUTABLE", "registryId": "123456789012"}]}),
            ("describe-images", {"imageDetails": [{"imageTags": ["a" * 40], "imageDigest": "sha256:" + "c" * 64}]}),
            ("describe-images", {"imageDetails": []}),
        ]:
            with self.subTest(operation=operation), self.assertRaises(builder.AmiBuildError):
                builder.verify_published_images(self.config, self.images,
                                                lambda *args: response if args[1] == operation else call(*args))

    def test_candidate_requires_provider_tags_encryption_owner_and_runtime(self):
        values = builder.source_inputs(self.config, self.images, self.root)
        tags = {"Module": "gitlab-runner-image-builder", "OperationId": self.config["operation_id"], "SourceRevision": "a" * 40}
        for name in ("bootstrap", "images", "image-verifier"):
            tags["adp:gitlab-runner:" + name + "-sha256"] = values[name.replace("-", "_") + "_sha256"]
        image = {"ImageId": "ami-00000001", "OwnerId": "123456789012", "State": "available", "Public": False,
                 "Architecture": "x86_64", "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
                 "BlockDeviceMappings": [{"Ebs": {"Encrypted": True}}]}
        manifest = {"builds": [{"artifact_id": "us-east-1:ami-00000001"}]}
        runtime = {"image_check_after_docker_restart": True, "docker_engine_version": "25.0.0"}
        candidate = builder.verify_candidate(self.config, values, manifest, runtime, lambda *args: {"Images": [image]})
        self.assertEqual(candidate["image_id"], "ami-00000001")
        for change in ({"OwnerId": "000000000000"}, {"Public": True}, {"Tags": []},
                       {"BlockDeviceMappings": []}, {"BlockDeviceMappings": [{"Ebs": {"Encrypted": False}}]}):
            with self.subTest(change=change), self.assertRaises(builder.AmiBuildError):
                builder.verify_candidate(self.config, values, manifest, runtime, lambda *args: {"Images": [{**image, **change}]})
        with self.assertRaises(builder.AmiBuildError):
            builder.verify_candidate(self.config, values, manifest, {**runtime, "image_check_after_docker_restart": False}, lambda *args: {"Images": [image]})



class BuilderCleanupContainerTests(unittest.TestCase):
    """Exercise real deletion in a disposable filesystem; service commands are fixtures."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker") or subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20).returncode:
            raise unittest.SkipTest("An isolated Docker daemon is required for cleanup filesystem tests")
        cls.image = json.loads((SCRIPT.parents[1] / "versions.lock.json").read_text())["bootstrap_python_image"]
        if subprocess.run(["docker", "image", "inspect", cls.image], capture_output=True, timeout=20).returncode:
            subprocess.run(["docker", "pull", cls.image], capture_output=True, check=True, timeout=120)

    def run_cleanup(self, mode):
        name = "adp-cleanup-test-" + uuid.uuid4().hex
        script = SCRIPT.parents[1] / "image/scrub-builder.sh"
        fixture = Path(__file__).parent / "fixtures/cleanup_container.py"
        try:
            result = subprocess.run([
                "docker", "run", "--rm", "-i", "--name", name, "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--entrypoint", "python3", self.image, "-", mode,
            ], input="SCRUB_SOURCE = " + repr(script.read_text()) + "\n" + fixture.read_text(),
                capture_output=True, text=True, check=True, timeout=60)
            return json.loads(result.stdout)
        finally:
            # This unique name belongs only to this test's ephemeral container.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)

    def test_stopped_writer_cleanup_removes_identity_and_records_completion(self):
        row = self.run_cleanup("inactive")
        self.assertEqual(row["returncode"], 0)
        self.assertEqual(row["remaining"], ["/etc/machine-id"])
        self.assertTrue(row["fresh_machine_id_mountpoint"])
        self.assertTrue(row["marker"])
        self.assertEqual(row["calls"], [
            ["is-active", "--quiet", "amazon-ssm-agent.service"],
            ["--no-reload", "mask", "amazon-ssm-agent.service"],
            ["cloud-init", "clean", "--logs", "--seed"],
        ])

    def test_active_writer_missing_ownership_or_failed_mask_never_deletes_identity(self):
        for mode in ("active", "no_marker", "mask_fails"):
            with self.subTest(mode=mode):
                row = self.run_cleanup(mode)
                self.assertNotEqual(row["returncode"], 0)
                self.assertEqual(len(row["remaining"]), 11)
                self.assertFalse(row["marker"])
                self.assertFalse(row["fresh_machine_id_mountpoint"])
                self.assertFalse(any(call[0] == "cloud-init" for call in row["calls"]))

if __name__ == "__main__":
    unittest.main()
