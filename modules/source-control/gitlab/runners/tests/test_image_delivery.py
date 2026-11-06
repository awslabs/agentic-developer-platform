"""Exercise the shipped image verifier and the host's fail-closed admission order."""

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_images", ROOT / "bootstrap/verify_images.py")
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)
JOB = "registry.example.test/job:v1@sha256:" + "a" * 64
HELPER = "registry.example.test/helper:v1@sha256:" + "b" * 64


def metadata(reference):
    repository, digest = reference.split("@sha256:")
    return {"RepoDigests": [repository.split(":", 1)[0] + "@sha256:" + digest],
            "Os": "linux", "Architecture": "amd64",
            "Config": {"User": "10001:10001"}}


class ImageDeliveryTests(unittest.TestCase):
    def test_exact_local_digest_and_nonroot_image_contract(self):
        transport = Mock(side_effect=lambda command, **kwargs: Mock(
            returncode=0, stdout=json.dumps([metadata(command[-1])]).encode()))
        images.verify([JOB, HELPER], run=transport)
        self.assertEqual([call.args[0] for call in transport.call_args_list],
                         [["docker", "image", "inspect", JOB], ["docker", "image", "inspect", HELPER]])

    def test_wrong_digest_root_or_architecture_refuses_admission(self):
        for change in (
            lambda data: data.update(RepoDigests=["other@sha256:" + "0" * 64]),
            lambda data: data["Config"].update(User="0:0"),
            lambda data: data.update(Architecture="arm64"),
        ):
            with self.subTest(change=change):
                transport = Mock(side_effect=lambda command, **kwargs: Mock(
                    returncode=0, stdout=json.dumps([data]).encode()))
                data = metadata(HELPER)
                change(data)
                with self.assertRaises(ValueError):
                    images.verify([JOB, HELPER], run=transport)
        for references in ([JOB, JOB], [JOB, "latest"], [JOB]):
            with self.subTest(references=references), self.assertRaises(ValueError):
                images.verify(references, run=Mock())
        with self.assertRaises(ValueError):
            images.verify([JOB, HELPER], run=Mock(return_value=Mock(returncode=1, stdout=b"")))

    def test_host_does_not_open_ssh_before_verifying_local_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            host = root / "adp-job-host"
            host.write_text("#!/bin/sh\nprintf 'host %s\\n' \"$1\" >> \"$ADP_CALLS\"\n")
            manager = binaries / "systemctl"
            manager.write_text("#!/bin/sh\nprintf 'systemctl %s %s\\n' \"$1\" \"$2\" >> \"$ADP_CALLS\"\n"
                               "test \"$1 $2\" != \"${ADP_SYSTEMCTL_FAIL:-}\"\n")
            docker = binaries / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                              "reference = sys.argv[-1]\n"
                              "if os.environ.get('ADP_MISSING') == reference: sys.exit(1)\n"
                              "repository, digest = reference.split('@sha256:')\n"
                              "canonical = repository.split(':', 1)[0] + '@sha256:' + digest\n"
                              "print(json.dumps([{'RepoDigests': [canonical], 'Os': 'linux', 'Architecture': 'amd64', "
                              "'Config': {'User': '10001:10001'}}]))\n")
            for executable in (host, manager, docker):
                executable.chmod(0o700)
            verifier = root / "adp-job-images"
            verifier.write_bytes((ROOT / "bootstrap/verify_images.py").read_bytes())
            calls = root / "calls"
            script = (ROOT / "templates/job-network-activate.sh.tftpl").read_text()
            parameters = {"host_sha256": hashlib.sha256(host.read_bytes()).hexdigest(),
                          "image_verifier_sha256": hashlib.sha256(verifier.read_bytes()).hexdigest(),
                          "job_image": JOB, "helper_image": HELPER,
                          "gitlab_ip": "10.0.0.10", "cache_ip": "10.0.0.12",
                          "gitlab_host": "gitlab.example.test",
                          "cache_host": "s3.us-east-1.amazonaws.com"}
            for key, value in parameters.items():
                script = script.replace("${" + key + "}", value)
            script = script.replace("/usr/local/libexec/adp-job-host", str(host))
            script = script.replace("/usr/local/libexec/adp-job-images", str(verifier))
            script = script.replace("/etc/hosts", str(root / "hosts"))
            script = script.replace("/run/adp-job-ready", str(root / "ready"))
            entrypoint = root / "boot.sh"
            entrypoint.write_text(script)
            environment = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}", ADP_CALLS=str(calls))
            for missing, failure, success in ((HELPER, "", False), (None, "", True),
                    (None, "start adp-job-observer.service", False),
                    (None, "stop adp-job-observer.service", False),
                    (None, "restart adp-job-firewall.service", False),
                    (None, "restart docker.service", False)):
                calls.write_text("")
                environment["ADP_MISSING"] = missing or ""
                environment["ADP_SYSTEMCTL_FAIL"] = failure
                result = subprocess.run(["bash", str(entrypoint)], env=environment,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                history = calls.read_text()
                if success:
                    self.assertEqual(history.count("systemctl start adp-job-observer.service"), 2)
                    self.assertLess(history.index("systemctl start adp-job-observer.service"), history.index("host --allow"))
                    self.assertLess(history.index("systemctl stop adp-job-observer.service"),
                                    history.index("systemctl restart adp-job-firewall.service"))
                    self.assertLess(history.index("systemctl restart adp-job-firewall.service"),
                                    history.index("systemctl restart docker.service"))
                    self.assertLess(history.index("systemctl restart docker.service"),
                                    history.index("host --ready"))
                    self.assertLess(history.index("host --ready"), history.index("systemctl restart sshd.service"))
                else:
                    self.assertNotIn("host --ready", history)
                    self.assertNotIn("systemctl restart sshd.service", history)


if __name__ == "__main__":
    unittest.main()
