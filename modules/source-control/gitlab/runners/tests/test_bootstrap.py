"""Offline fixtures for runtime-only runner secrets and strict CA loading."""

import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

MODULE = Path(__file__).resolve().parents[1] / "bootstrap" / "load_credentials.py"
SPEC = importlib.util.spec_from_file_location("load_credentials", MODULE)
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)


def certificate(common_name, *, expired=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=30))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class FakeSecrets:
    def __init__(self, token, ca):
        self.objects = {
            "token-arn": {"SecretString": json.dumps(token), "VersionId": "operation-id"},
            "ca-arn": {"SecretString": ca},
        }
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        return self.objects[kwargs["SecretId"]]


class VerifiedConnection:
    def __init__(self, ca, system_id_file, *, code=200, runner_id=101, token="glrt-synthetic-canary"):
        self.runner_id = runner_id
        self.token = token
        self.request_token = token
        self.system_id_file = system_id_file
        self.ca = ca
        self.code = code
        self.options = None

    def __call__(self, hostname, *, context, timeout):
        self.options = (hostname, timeout, context)
        return self

    def request(self, method, path, *, body, headers):
        fields = parse_qs(body)
        if ((method, path) != ("POST", "/gitlab/api/v4/runners/verify")
                or fields.get("token") != [self.request_token]
                or fields.get("system_id") != [self.system_id_file.read_text().strip()]):
            raise AssertionError("invalid verification request")
        if headers != {"Content-Type": "application/x-www-form-urlencoded"}:
            raise AssertionError("invalid verification headers")

    def getresponse(self):
        with tempfile.TemporaryDirectory() as temporary:
            leaf = Path(temporary) / "leaf.pem"
            roots = Path(temporary) / "roots.pem"
            leaf.write_text(self.ca)
            for certificate in self.options[2].get_ca_certs(binary_form=True):
                roots.write_bytes(x509.load_der_x509_certificate(certificate).public_bytes(serialization.Encoding.PEM))
            result = subprocess.run(["openssl", "verify", "-CAfile", str(roots),
                                     "-verify_hostname", self.options[0], str(leaf)],
                                    capture_output=True, check=False)
            if result.returncode:
                raise OSError("synthetic TLS verification failed")
        return type("Reply", (), {"status": self.code, "read": lambda _, length: json.dumps(
            {"id": self.runner_id, "token": self.token}).encode()[:length]})()

    def close(self):
        pass


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "secrets"
        self.certs = Path(self.temporary.name) / "certs"
        self.identity = Path(self.temporary.name) / "identity"
        for directory in (self.output, self.certs, self.identity):
            directory.mkdir()
        self.ca = certificate("gitlab.example.test")
        self.token = {
            "schema_version": 1, "group_id": 42, "tenant_id": "tenant-example", "runner_id": 101,
            "generation": 1, "runner_url": "https://gitlab.example.test/gitlab",
            "tag_list": ["adp-group-42"], "access_level": "ref_protected",
            "token": "glrt-synthetic-canary", "operation_id": "operation-id",
            "token_expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        self.secrets = FakeSecrets(self.token, self.ca)
        self.client = VerifiedConnection(self.ca, self.identity / ".runner_system_id")

    def run_loader(self, **changes):
        options = dict(secrets=self.secrets, group_id=42, tenant_id="tenant-example",
                       runner_url="https://gitlab.example.test/gitlab", tags=["adp-group-42"],
                       access_level="ref_protected", token_arn="token-arn", ca_arn="ca-arn",
                       output_dir=self.output, certs_dir=self.certs, system_id_dir=self.identity,
                       connection_factory=self.client)
        options.update(changes)
        loader.load(**options)

    def invoke_cli(self):
        arguments = ["load_credentials.py", "--group-id", "42", "--tenant-id", "tenant-example",
                     "--runner-url", "https://gitlab.example.test/gitlab", "--tags", "adp-group-42",
                     "--access-level", "ref_protected", "--token-arn", "token-arn", "--ca-arn", "ca-arn",
                     "--output-dir", str(self.output), "--certs-dir", str(self.certs),
                     "--system-id-dir", str(self.identity)]
        actual_load = loader.load

        def load_with_test_connection(*args):
            return actual_load(*args, connection_factory=self.client)

        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(sys, "argv", arguments), patch.object(loader.boto3, "client", return_value=self.secrets),
              patch.object(loader, "load", side_effect=load_with_test_connection),
              redirect_stdout(output), redirect_stderr(errors)):
            result = loader.main()
        return result, output.getvalue(), errors.getvalue()

    def test_runtime_canary_is_only_in_memory_mounts_not_logs_or_receipt(self):
        result, output, errors = self.invoke_cli()
        self.assertEqual(result, 0)
        self.assertEqual((output, errors), ("", ""))
        self.assertEqual((self.output / "runner-token").read_text(), self.token["token"])
        self.assertEqual((self.output / "ca.pem").read_text(), self.ca)
        receipt = (self.identity / ".adp-verified-runner.json").read_text()
        self.assertNotIn(self.token["token"], receipt)
        self.assertNotIn(self.ca, receipt)

    def test_failed_verification_redacts_canary_and_response(self):
        self.client.token = "glrt-untrusted-response-canary"
        result, output, errors = self.invoke_cli()
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertEqual(errors, "private_runner_not_verified\n")
        for leaked in (self.token["token"], self.client.token, self.ca):
            self.assertNotIn(leaked, output + errors)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.certs.iterdir()), [])
        self.assertEqual(list(self.identity.iterdir()), [])

    def test_routable_runner_token_loads_only_after_private_verification(self):
        token = "glrt-c3ludGhldGljX3J1bm5lcl9jYW5hcnk.01.0v0abcd12"
        self.token["token"] = token
        self.secrets = FakeSecrets(self.token, self.ca)
        self.client = VerifiedConnection(self.ca, self.identity / ".runner_system_id", token=token)
        result, output, errors = self.invoke_cli()
        self.assertEqual((result, output, errors), (0, "", ""))
        self.assertEqual((self.output / "runner-token").read_text(), token)
        self.assertEqual(self.client.options[:2], ("gitlab.example.test", 10))
        self.assertNotIn(token, (self.identity / ".adp-verified-runner.json").read_text())

    def test_routable_runner_token_keeps_exact_private_runner_identity(self):
        token = "glrt-c3ludGhldGljX3J1bm5lcl9jYW5hcnk.01.0v0abcd12"
        self.token["token"] = token
        self.secrets = FakeSecrets(self.token, self.ca)
        self.client = VerifiedConnection(self.ca, self.identity / ".runner_system_id", token=token, runner_id=102)
        result, output, errors = self.invoke_cli()
        self.assertEqual((result, output, errors), (1, "", "private_runner_not_verified\n"))
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.certs.iterdir()), [])
        self.assertEqual(list(self.identity.iterdir()), [])

    def test_malformed_routable_tokens_are_rejected_without_leaking(self):
        for token in (
            "glrt-payload.01.short", "glrt-payload.02.0v0abcd12",
            "glrt-payload.01.0V0abcd12", "glrt-payload.01.0v0abcd12.extra",
            "glrt-payload=.01.0v0abcd12", "glrt-.01.0v0abcd12",
            "glrt-payload.01.0v0abcd12\n", "glrt-payload.01.0v0abcd12\x00",
        ):
            with self.subTest(token=token):
                self.token["token"] = token
                self.secrets = FakeSecrets(self.token, self.ca)
                result, output, errors = self.invoke_cli()
                self.assertEqual((result, output, errors), (1, "", "runner_secret_not_verified\n"))
                self.assertEqual(list(self.output.iterdir()), [])

    def test_verified_secret_is_written_only_after_tls_check(self):
        self.run_loader()
        self.assertEqual((self.output / "runner-token").read_text(), "glrt-synthetic-canary")
        self.assertEqual((self.output / "ca.pem").read_text(), self.ca)
        self.assertEqual(stat.S_IMODE((self.output / "runner-token").stat().st_mode), 0o400)
        self.assertEqual((self.certs / "gitlab.example.test.crt").read_text(), self.ca)
        self.assertEqual(stat.S_IMODE((self.certs / "gitlab.example.test.crt").stat().st_mode), 0o400)
        system_id = (self.identity / ".runner_system_id").read_text().strip()
        self.assertRegex(system_id, r"^r_[0-9a-f]{12}$")
        self.assertEqual(stat.S_IMODE((self.identity / ".runner_system_id").stat().st_mode), 0o400)
        self.assertEqual(json.loads((self.identity / ".adp-verified-runner.json").read_text()),
                         {"runner_id": 101, "system_id": system_id})
        self.assertNotIn("glrt-", (self.identity / ".adp-verified-runner.json").read_text())
        self.assertEqual(self.secrets.calls, [
            {"SecretId": "token-arn", "VersionStage": "AWSCURRENT"},
            {"SecretId": "ca-arn", "VersionStage": "AWSCURRENT"},
        ])
        self.assertEqual(self.client.options[:2], ("gitlab.example.test", 10))

    def test_wrong_group_and_secret_version_are_rejected(self):
        for change in ({"group_id": 43}, {"tags": ["adp-group-43"]}):
            with self.subTest(change=change), self.assertRaisesRegex(loader.BootstrapError, "runner_secret_not_verified"):
                self.run_loader(**change)
        self.secrets.objects["token-arn"]["VersionId"] = "other-version"
        with self.assertRaisesRegex(loader.BootstrapError, "runner_secret_not_verified"):
            self.run_loader()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_wrong_or_expired_ca_cannot_activate_runner(self):
        for ca in (certificate("different-root"), certificate("gitlab.example.test", expired=True), ""):
            self.secrets.objects["ca-arn"]["SecretString"] = ca
            with self.subTest(ca=ca[:15]), self.assertRaises(loader.BootstrapError):
                self.run_loader()
            self.assertEqual(list(self.output.iterdir()), [])
            self.assertEqual(list(self.certs.iterdir()), [])
            self.assertEqual(list(self.identity.iterdir()), [])

    def test_redirect_refused_before_token_write(self):
        self.client.code = 302
        with self.assertRaisesRegex(loader.BootstrapError, "private_runner_not_verified"):
            self.run_loader()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.certs.iterdir()), [])
        self.assertEqual(list(self.identity.iterdir()), [])

    def test_wrong_verified_runner_or_token_never_issues_receipt(self):
        for runner_id, token in ((102, "glrt-synthetic-canary"), (101, "glrt-other")):
            self.client.runner_id = runner_id
            self.client.token = token
            with self.subTest(runner_id=runner_id), self.assertRaisesRegex(
                    loader.BootstrapError, "private_runner_not_verified"):
                self.run_loader()
            self.assertEqual(list(self.output.iterdir()), [])
            self.assertEqual(list(self.certs.iterdir()), [])
            self.assertEqual(list(self.identity.iterdir()), [])

    def test_http_and_unexpected_path_refused_without_fetch(self):
        for url in ("http://gitlab.example.test/gitlab", "https://gitlab.example.test/other",
                    "https://gitlab.example.test:8443/gitlab", "https://gitlab.example.test:invalid/gitlab",
                    "https://user@gitlab.example.test/gitlab"):
            with self.subTest(url=url), self.assertRaisesRegex(loader.BootstrapError, "runner_url_not_verified"):
                self.run_loader(runner_url=url)
        self.assertEqual(self.secrets.calls, [])

    def test_missing_ca_secret_and_invalid_token_are_safe(self):
        self.secrets.objects.pop("ca-arn")
        with self.assertRaisesRegex(loader.BootstrapError, "runtime_secret_load_failed"):
            self.run_loader()
        self.secrets.objects["ca-arn"] = {"SecretString": self.ca}
        self.token["token"] = "unexpected\u2603token"
        self.secrets.objects["token-arn"]["SecretString"] = json.dumps(self.token)
        with self.assertRaisesRegex(loader.BootstrapError, "runner_secret_not_verified"):
            self.run_loader()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_mount_must_be_empty(self):
        (self.output / "existing").write_text("fixture")
        with self.assertRaisesRegex(loader.BootstrapError, "secret_mount_not_empty"):
            self.run_loader()
        self.assertFalse((self.output / "runner-token").exists())


if __name__ == "__main__":
    unittest.main()
