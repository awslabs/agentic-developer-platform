"""Offline release gate fixtures; no AWS calls or deployable synthetic artifacts."""

import copy
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_lock", ROOT / "scripts" / "release_lock.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseLockTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((ROOT / "versions.lock.json").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("assets", "bootstrap", "templates"):
            (self.root / directory).mkdir()
        (self.root / "assets" / "gitlab-runner-0.94.2.tgz").write_bytes(
            (ROOT / "assets" / "gitlab-runner-0.94.2.tgz").read_bytes())
        requirements = b"synthetic offline requirements fixture\n"
        bootstrap = b"synthetic offline host fixture\n"
        (self.root / "bootstrap" / "requirements.lock").write_bytes(requirements)
        (self.root / "templates" / "job-user-data.sh.tftpl").write_bytes(bootstrap)
        (self.root / "templates" / "runner.toml.tftpl").write_bytes(
            (ROOT / "templates" / "runner.toml.tftpl").read_bytes())
        (self.root / "bootstrap" / "nonroot-helper.Dockerfile").write_bytes(
            (ROOT / "bootstrap" / "nonroot-helper.Dockerfile").read_bytes())
        (self.root / "bootstrap" / "verify_images.py").write_bytes(
            (ROOT / "bootstrap" / "verify_images.py").read_bytes())
        (self.root / "scripts").mkdir()
        for folder, filename in (("scripts", "prepare_manager.py"), ("templates", "manager-prestart.sh"),
                                 ("bootstrap", "install_plugin.py")):
            (self.root / folder / filename).write_bytes((ROOT / folder / filename).read_bytes())
        (self.root / "bootstrap" / "load_credentials.py").write_bytes(
            (ROOT / "bootstrap" / "load_credentials.py").read_bytes())
        self.lock = copy.deepcopy(self.source)
        self.lock["compatibility_reviewed"] = True
        self.lock["bootstrap_requirements_sha256"] = hashlib.sha256(requirements).hexdigest()
        self.lock["helper_image"] = "example.invalid/nonroot-helper:v1@sha256:" + "a" * 64
        self.lock["helper_dockerfile_sha256"] = release.digest(self.root / "bootstrap" / "nonroot-helper.Dockerfile")
        self.lock["image_verifier_sha256"] = release.digest(self.root / "bootstrap" / "verify_images.py")
        self.lock["manager_guard_sha256"] = release.digest(self.root / "scripts" / "prepare_manager.py")
        self.lock["manager_prestart_sha256"] = release.digest(self.root / "templates" / "manager-prestart.sh")
        self.lock["plugin_installer_sha256"] = release.digest(self.root / "bootstrap" / "install_plugin.py")
        self.lock["bootstrap_image"] = "example.invalid/loader:v1@sha256:" + "b" * 64
        self.lock["loader_source_sha256"] = release.digest(self.root / "bootstrap" / "load_credentials.py")
        self.lock["probe_image"] = "example.invalid/probe:v1@sha256:" + "c" * 64
        self.lock["job_image"] = "example.invalid/job:v1@sha256:" + "d" * 64
        self.lock["host_ami"] = {"us-east-1": {
            "image_id": "ami-0123456789abcdef0", "owner_id": "123456789012",
            "architecture": "x86_64", "docker_engine_version": "1.2.3",
            "bootstrap_sha256": hashlib.sha256(bootstrap).hexdigest(),
            "images_sha256": hashlib.sha256((self.lock["job_image"] + "\n" + self.lock["helper_image"] + "\n").encode()).hexdigest(),
            "image_verifier_sha256": self.lock["image_verifier_sha256"],
        }}

    def check(self):
        return release.check(self.lock, "us-east-1", "ami-0123456789abcdef0", self.lock["job_image"], self.root)

    def test_pinned_local_image_rejects_unreviewed_registry_digest(self):
        unreviewed = self.lock["job_image"].replace(":v1@", ":v2@")
        self.assertNotEqual(unreviewed, self.lock["job_image"])
        with self.assertRaises(release.ReleaseError):
            release.check(self.lock, "us-east-1", "ami-0123456789abcdef0", unreviewed, self.root)

    def test_real_upstream_lock_is_explicitly_not_deployable(self):
        with self.assertRaises(release.ReleaseError):
            release.check(self.source, "us-east-1", "ami-0123456789abcdef0", "", ROOT)
        self.assertFalse(self.source["compatibility_reviewed"])
        self.assertEqual(release.digest(ROOT / "assets" / "gitlab-runner-0.94.2.tgz"), self.source["chart"]["sha256"])
        self.assertNotIn("helper_image", self.source)
        self.assertEqual(self.source["host_ami"], {})

    def test_external_program_refuses_current_incomplete_release_without_aws(self):
        result = subprocess.run(["python3", str(ROOT / "scripts" / "release_lock.py")],
                                input=json.dumps({"region": "us-east-1", "host_ami_id": "",
                                                  "job_image": ""}), text=True,
                                capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"ready": "false"})
        self.assertEqual(result.stderr, "")

    def test_synthetic_complete_contract_can_be_checked_offline(self):
        self.assertEqual(self.check()["owner_id"], "123456789012")

    def test_clone_url_is_pinned_to_private_rooted_host(self):
        template = self.root / "templates" / "runner.toml.tftpl"
        original = template.read_text()
        expected = '  clone_url = "https://${gitlab_host}/gitlab"\n'
        self.assertIn(expected, original)
        for replacement in ('', '  clone_url = "https://public.example.test/gitlab"\n',
                            '  clone_url = "http://${gitlab_host}/gitlab"\n',
                            '  clone_url = "https://${gitlab_host}"\n'):
            with self.subTest(replacement=replacement):
                template.write_text(original.replace(expected, replacement, 1))
                with self.assertRaises(release.ReleaseError):
                    self.check()
        template.write_text(original)

    def test_missing_or_floating_artifacts_and_wrong_source_are_refused(self):
        for change in (lambda lock: lock.pop("probe_image"),
                       lambda lock: lock.update(compatibility_reviewed=False),
                       lambda lock: lock.update(job_image="example.invalid/job:latest"),
                       lambda lock: lock["host_ami"]["us-east-1"].update(owner_id=""),
                       lambda lock: lock["fleeting_plugin"].update(sha256="0" * 64)):
            original = copy.deepcopy(self.lock)
            change(self.lock)
            with self.subTest(change=change), self.assertRaises((release.ReleaseError, KeyError)):
                self.check()
            self.lock = original

    def test_loader_source_must_match_reviewed_lock(self):
        self.lock["loader_source_sha256"] = "0" * 64
        with self.assertRaises(release.ReleaseError):
            self.check()
        self.lock.pop("loader_source_sha256")
        with self.assertRaises((release.ReleaseError, KeyError)):
            self.check()

    def test_plugin_installer_requires_a_matching_release_hash(self):
        installer = self.root / "bootstrap" / "install_plugin.py"
        original = installer.read_bytes()
        installer.write_bytes(original + b"unsafe override")
        with self.assertRaises(release.ReleaseError):
            self.check()
        installer.write_bytes(original)
        self.lock["plugin_installer_sha256"] = "0" * 64
        with self.assertRaises(release.ReleaseError):
            self.check()
        self.lock.pop("plugin_installer_sha256")
        with self.assertRaises((release.ReleaseError, KeyError)):
            self.check()

    def test_wrong_local_hash_and_ami_mapping_are_refused(self):
        with self.assertRaises(release.ReleaseError):
            release.check(self.lock, "us-west-2", "ami-0123456789abcdef0", self.lock["job_image"], self.root)
        with self.assertRaises(release.ReleaseError):
            release.check(self.lock, "us-east-1", "ami-fffffffffffffffff", self.lock["job_image"], self.root)
        (self.root / "templates" / "job-user-data.sh.tftpl").write_bytes(b"changed")
        with self.assertRaises(release.ReleaseError):
            self.check()

    def test_dangerous_executor_overrides_and_debug_trace_are_rejected(self):
        template = self.root / "templates" / "runner.toml.tftpl"
        original = template.read_text()
        alterations = (
            ('ServerAddress = "s3.${aws_region}.amazonaws.com"', 'ServerAddress = "s3.amazonaws.com"'),
            ('PathStyle = true', 'PathStyle = false'),
            ('s3.${aws_region}.amazonaws.com:${cache_ip}', 's3.amazonaws.com:8.8.8.8'),
            ('services_limit = 0', 'services_limit = -1'),
            ('services_limit = 0', 'services_limit = false'),
            ('user = "10001:10001"', 'user = "0"'),
            ('allowed_users = ["10001:10001"]', 'allowed_users = ["*"]'),
            ('allowed_images = ["${image}"]', 'allowed_images = ["*"]'),
            ('helper_image = "${helper_image}"', 'helper_image = "gitlab/gitlab-runner-helper:latest"'),
            ('privileged = false', 'privileged = true'),
            ('services_privileged = false', 'services_privileged = true'),
            ('cap_drop = ["ALL"]', 'cap_drop = []'),
            ('services_cap_drop = ["ALL"]', 'services_cap_drop = []'),
            ('security_opt = ["no-new-privileges:true"]', 'security_opt = []'),
            ('disable_entrypoint_overwrite = true', 'disable_entrypoint_overwrite = false'),
            ('debug_trace_disabled = true', 'debug_trace_disabled = false'),
            ('allowed_pull_policies = ["never"]', 'allowed_pull_policies = ["always"]'),
            ('volumes = ["/builds", "/cache"]', 'volumes = ["/var/run/docker.sock:/var/run/docker.sock"]'),
            ('services_limit = 0', 'services_limit = 0\n    allowed_services = ["*"]'),
            ('services_limit = 0', 'services_limit = 0\n    network_mode = "host"'),
            ('services_limit = 0', 'services_limit = 0\n    pid_mode = "host"'),
            ('services_limit = 0', 'services_limit = 0\n    ipcmode = "host"'),
            ('services_limit = 0', 'services_limit = 0\n    volumes = ["/var/run/docker.sock:/var/run/docker.sock"]'),
            ('services_limit = 0', 'services_limit = 0\n    devices = ["/dev/kmsg:/dev/kmsg"]'),
            ('services_limit = 0', 'services_limit = 0\n    extra_hosts = ["metadata:169.254.169.254"]'),
            ('extra_hosts = ["${gitlab_host}:${gitlab_ip}", "s3.${aws_region}.amazonaws.com:${cache_ip}"]',
             'extra_hosts = ["${gitlab_host}:${gitlab_ip}", "${registry_host}:${registry_ip}", "s3.${aws_region}.amazonaws.com:${cache_ip}"]'),
            ('services_limit = 0', 'services_limit = 0\n    cap_add = ["NET_ADMIN"]'),
            ('services_limit = 0', 'services_limit = 0\n    environment = ["AWS_ACCESS_KEY_ID=example"]'),
        )
        for old, new in alterations:
            with self.subTest(override=new):
                self.assertIn(old, original)
                template.write_text(original.replace(old, new, 1))
                with self.assertRaises((release.ReleaseError, ValueError)):
                    self.check()
        template.write_text(original)

    def test_one_use_limits_and_guard_sources_are_required(self):
        template = self.root / "templates" / "runner.toml.tftpl"
        original = template.read_text()
        for old, replacement in (("max_use_count = 1", "max_use_count = 2"),
                                 ("idle_count = 0", "idle_count = 1"),
                                 ("max_instances = 2", "max_instances = 3"),
                                 ("capacity_per_instance = 1", "capacity_per_instance = 2")):
            with self.subTest(replacement=replacement):
                template.write_text(original.replace(old, replacement, 1))
                with self.assertRaises(release.ReleaseError):
                    self.check()
        template.write_text(original)
        for source in (self.root / "scripts" / "prepare_manager.py",
                       self.root / "templates" / "manager-prestart.sh"):
            with self.subTest(source=source):
                source.write_bytes(source.read_bytes() + b"unsafe override")
                with self.assertRaises(release.ReleaseError):
                    self.check()

    def test_helper_must_be_built_from_reviewed_nonroot_source(self):
        helper = self.root / "bootstrap" / "nonroot-helper.Dockerfile"
        original = helper.read_text()
        for old, replacement in (("USER 10001:10001", "USER 0:0"),
                                 ("/gitlab-runner-cache-init", ""),
                                 ("RUN install -d -m 0700 -o 10001 -g 10001 /builds /cache /gitlab-runner-cache-init\n", "")):
            with self.subTest(replacement=replacement):
                helper.write_text(original.replace(old, replacement))
                self.lock["helper_dockerfile_sha256"] = release.digest(helper)
                with self.assertRaises(release.ReleaseError):
                    self.check()

    def test_image_manifest_and_verifier_attestations_are_required(self):
        for field in ("images_sha256", "image_verifier_sha256"):
            with self.subTest(field=field):
                self.lock["host_ami"]["us-east-1"][field] = "0" * 64
                with self.assertRaises(release.ReleaseError):
                    self.check()
                self.lock["host_ami"]["us-east-1"][field] = (
                    hashlib.sha256((self.lock["job_image"] + "\n" + self.lock["helper_image"] + "\n").encode()).hexdigest()
                    if field == "images_sha256" else self.lock["image_verifier_sha256"])
        (self.root / "bootstrap/verify_images.py").write_text("pass\n")
        with self.assertRaises(release.ReleaseError):
            self.check()

    def test_ami_owner_architecture_public_and_state_are_checked_via_transport(self):
        host = self.check()
        image = {"ImageId": host["image_id"], "OwnerId": host["owner_id"],
                 "Architecture": "x86_64", "State": "available", "Public": False,
                 "Tags": [{"Key": "adp:gitlab-runner:" + key + "-sha256", "Value": host[value]}
                          for key, value in (("bootstrap", "bootstrap_sha256"), ("images", "images_sha256"),
                                             ("image-verifier", "image_verifier_sha256"))]}
        transport = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"Images": [image]}).encode()))
        release.verify_ami(host, "us-east-1", run=transport)
        self.assertIn(host["owner_id"], transport.call_args.args[0])
        for tags in ([], *([dict(tag, Value="0" * 64) if tag["Key"] == target else tag
                            for tag in image["Tags"]] for target in
                           ("adp:gitlab-runner:bootstrap-sha256", "adp:gitlab-runner:images-sha256",
                            "adp:gitlab-runner:image-verifier-sha256"))):
            transport.return_value.stdout = json.dumps({"Images": [dict(image, Tags=tags)]}).encode()
            with self.subTest(tags=tags), self.assertRaises(release.ReleaseError):
                release.verify_ami(host, "us-east-1", run=transport)
        for field, value in (("OwnerId", "000000000000"), ("Architecture", "arm64"),
                             ("Public", True), ("State", "pending")):
            changed = dict(image, **{field: value})
            transport.return_value.stdout = json.dumps({"Images": [changed]}).encode()
            with self.subTest(field=field), self.assertRaises(release.ReleaseError):
                release.verify_ami(host, "us-east-1", run=transport)
