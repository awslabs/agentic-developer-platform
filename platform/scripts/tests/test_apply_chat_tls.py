"""apply-chat-tls.sh and chat-tls-issuer.py: issuer handling, rendering and idempotent apply (#7899, SCN-02).

The issuer helper's generated material is checked with render-chat-tls.py's own
validation. The bash applier runs end to end against stub `kubectl`/`aws`
binaries and a stub issuer step, so the temp-file hygiene, the CA ConfigMap
name, the SSM publication and the idempotent re-run are exercised for real.
"""

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


issuer = load("chat-tls-issuer.py")
renderer = load("render-chat-tls.py")
NOW = datetime(2026, 10, 16, tzinfo=UTC)


def not_found():
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "GetSecretValue")


class IssuerTests(unittest.TestCase):
    def test_generated_material_passes_the_renderer_validation_with_the_fixed_hostname(self):
        leaf, key, ca, ca_key = issuer.generate("dev", NOW)
        name = renderer.validate_certificate(leaf, key, ca, NOW)
        self.assertRegex(name, r"^chat-sandbox-gateway-ca-[a-f0-9]{16}$")
        # Validity: CA two years, leaf one year; still valid well past 48h.
        renderer.validate_certificate(leaf, key, ca, NOW + timedelta(days=360))
        with self.assertRaises(ValueError):
            renderer.validate_certificate(leaf, key, ca, NOW + timedelta(days=364))
        # The CA key signs the CA; it is a distinct PKCS8 PEM from the leaf key.
        self.assertTrue(ca_key.startswith(b"-----BEGIN PRIVATE KEY-----"))
        self.assertNotEqual(ca_key, key)
        with self.assertRaises(ValueError):
            renderer.validate_certificate(leaf, ca_key, ca, NOW)

    def test_missing_secret_generates_a_dev_only_issuer_and_stores_it_marked_not_for_production(self):
        secrets = Mock()
        secrets.get_secret_value.side_effect = not_found()
        leaf, key, ca, source = issuer.ensure(secrets, "adp/dev/chat-sandbox-gateway/issuer", "dev", NOW)
        self.assertEqual(source, "generated")
        renderer.validate_certificate(leaf, key, ca, NOW)
        secrets.create_secret.assert_called_once()
        call = secrets.create_secret.call_args.kwargs
        self.assertEqual(call["Name"], "adp/dev/chat-sandbox-gateway/issuer")
        self.assertIn("NOT for production", call["Description"])
        self.assertIn("Operator-owned", call["Description"])
        stored = json.loads(call["SecretString"])
        self.assertEqual(set(stored), {"ca_cert", "ca_key", "leaf_cert", "leaf_key"})
        self.assertEqual(stored["leaf_cert"].encode(), leaf)
        self.assertEqual(stored["ca_cert"].encode(), ca)
        self.assertIn({"Key": "adp:production", "Value": "false"}, call["Tags"])

    def test_existing_secret_is_reused_unchanged_and_validated(self):
        leaf, key, ca, ca_key = issuer.generate("dev", NOW)
        secrets = Mock()
        secrets.get_secret_value.return_value = {"SecretString": json.dumps({
            "ca_cert": ca.decode(), "ca_key": ca_key.decode(),
            "leaf_cert": leaf.decode(), "leaf_key": key.decode(),
        })}
        result = issuer.ensure(secrets, "adp/dev/chat-sandbox-gateway/issuer", "dev", NOW)
        self.assertEqual(result, (leaf, key, ca, "existing"))
        secrets.create_secret.assert_not_called()
        secrets.put_secret_value.assert_not_called()

    def test_unusable_or_malformed_stored_issuer_fails_closed_without_regenerating(self):
        leaf, key, ca, ca_key = issuer.generate("dev", NOW)
        _, foreign_key, _, _ = issuer.generate("dev", NOW)
        for payload, message in (
            ("not json", "not JSON"),
            (json.dumps({"ca_cert": ca.decode()}), "must contain"),
            (json.dumps({"ca_cert": ca.decode(), "ca_key": ca_key.decode(),
                         "leaf_cert": leaf.decode(), "leaf_key": foreign_key.decode()}), "not usable"),
        ):
            with self.subTest(message=message):
                secrets = Mock()
                secrets.get_secret_value.return_value = {"SecretString": payload}
                with self.assertRaisesRegex(ValueError, message):
                    issuer.ensure(secrets, "adp/dev/chat-sandbox-gateway/issuer", "dev", NOW)
                secrets.create_secret.assert_not_called()

    def test_other_service_errors_propagate(self):
        secrets = Mock()
        secrets.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetSecretValue")
        with self.assertRaises(ClientError):
            issuer.ensure(secrets, "adp/dev/chat-sandbox-gateway/issuer", "dev", NOW)

    def test_material_is_written_exclusively_with_private_permissions(self):
        leaf, key, ca, _ = issuer.generate("dev", NOW)
        with tempfile.TemporaryDirectory() as tmp:
            issuer.write_material(tmp, leaf, key, ca)
            for name in ("leaf.pem", "key.pem", "ca.pem"):
                mode = stat.S_IMODE(os.stat(Path(tmp) / name).st_mode)
                self.assertEqual(mode, 0o600, name)
            with self.assertRaises(FileExistsError):
                issuer.write_material(tmp, leaf, key, ca)


BASELINE = {
    "kind": "Deployment",
    "metadata": {"name": "bedrockgateway", "namespace": "adp-gateway"},
    "spec": {"template": {"spec": {
        "serviceAccountName": "gateway-service",
        "containers": [{
            "name": "bedrockgateway",
            "image": "111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64,
            "env": [{"name": "ADP_CHAT_DATA_ENABLED", "value": "false"}],
        }],
    }}},
}

# `aws` double: SSM parameters live in a JSON file so a second run observes the
# first run's publication. Everything else succeeds silently.
AWS_STUB = """#!/usr/bin/env bash
STATE="$TEST_SSM_STATE"
echo "aws $*" >> "$TEST_CALLS"
if [ "$1 $2" = "ssm get-parameter" ]; then
  python3 - "$STATE" "$4" <<'PY'
import json, sys
from pathlib import Path
state = json.loads(Path(sys.argv[1]).read_text()) if Path(sys.argv[1]).exists() else {}
if sys.argv[2] not in state: sys.exit(255)
print(state[sys.argv[2]])
PY
  exit $?
fi
if [ "$1 $2" = "ssm put-parameter" ]; then
  python3 - "$STATE" "$@" <<'PY'
import json, sys
from pathlib import Path
args = sys.argv[2:]
state = json.loads(Path(sys.argv[1]).read_text()) if Path(sys.argv[1]).exists() else {}
state[args[args.index("--name") + 1]] = args[args.index("--value") + 1]
Path(sys.argv[1]).write_text(json.dumps(state))
PY
  exit 0
fi
exit 0
"""

KUBECTL_STUB = """#!/usr/bin/env bash
echo "kubectl $*" >> "$TEST_CALLS"
case "$1 $2" in
  "get deployment") cat "$TEST_BASELINE" ;;
  "get service") echo "10.0.12.34" ;;
  "apply --server-side")
    # Record what was applied (kinds and names only; never the data).
    python3 - "${@: -1}" <<'PY' >> "$TEST_CALLS"
import json, sys
for item in json.load(open(sys.argv[1]))["items"]:
    print("applied", item["kind"], item["metadata"]["namespace"], item["metadata"]["name"])
PY
    ;;
esac
exit 0
"""


class ApplierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        scripts = self.tmp / "scripts"
        scripts.mkdir()
        shutil.copy(SCRIPTS / "apply-chat-tls.sh", scripts / "apply-chat-tls.sh")
        shutil.copy(SCRIPTS / "render-chat-tls.py", scripts / "render-chat-tls.py")
        leaf, key, ca, _ = issuer.generate("dev", datetime.now(UTC))
        self.expected_name = renderer.validate_certificate(leaf, key, ca)
        self.ca_pem = ca
        material = self.tmp / "material"
        material.mkdir()
        issuer.write_material(material, leaf, key, ca)
        # The issuer step is replaced by a copy of fixed material: Secrets Manager is
        # reached through boto3 inside the real helper and is covered by IssuerTests.
        stub = scripts / "chat-tls-issuer.py"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "out = sys.argv[sys.argv.index('--output-dir') + 1]\n"
            f"for name in ('leaf.pem', 'key.pem', 'ca.pem'): shutil.copy('{material}/' + name, out + '/' + name)\n"
            "print('issuer=existing')\n"
        )
        stub.chmod(0o755)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("aws", AWS_STUB), ("kubectl", KUBECTL_STUB)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.calls = self.tmp / "calls"
        self.baseline = self.tmp / "baseline.json"
        self.baseline.write_text(json.dumps(BASELINE))
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("AWS_")},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TEST_CALLS": str(self.calls),
            "TEST_SSM_STATE": str(self.tmp / "ssm.json"),
            "TEST_BASELINE": str(self.baseline),
            "TMPDIR": str(self.tmp / "work"),
        }
        (self.tmp / "work").mkdir()
        self.script = scripts / "apply-chat-tls.sh"

    def run_applier(self, *args):
        return subprocess.run(["bash", str(self.script), "--env", "dev", "--region", "us-east-1", *args],
                              capture_output=True, text=True, env=self.env)

    def test_renders_applies_publishes_and_prints_only_the_configmap_name(self):
        result = self.run_applier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.expected_name)
        calls = self.calls.read_text()
        for kind, namespace, name in (
            ("Secret", "adp-gateway", "chat-sandbox-gateway"),
            ("Deployment", "adp-gateway", "chat-sandbox-gateway"),
            ("Service", "adp-gateway", "chat-sandbox-gateway"),
            ("NetworkPolicy", "adp-gateway", "chat-sandbox-gateway"),
            ("ConfigMap", "adp-gateway-agents", self.expected_name),
            ("Role", "adp-gateway-agents", self.expected_name),
            ("RoleBinding", "adp-gateway-agents", self.expected_name),
        ):
            self.assertIn(f"applied {kind} {namespace} {name}", calls)
        self.assertIn("kubectl rollout status deployment/chat-sandbox-gateway -n adp-gateway", calls)
        self.assertIn(f"--name /adp/dev/gateway/chat-sandbox-ca-configmap --type String --value {self.expected_name} --overwrite", calls)
        self.assertEqual(json.loads((self.tmp / "ssm.json").read_text()),
                         {"/adp/dev/gateway/chat-sandbox-ca-configmap": self.expected_name})
        # No PEM content anywhere in the output, and the private work dir is gone.
        for text in (result.stdout, result.stderr, calls):
            self.assertNotIn("-----BEGIN", text)
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_second_run_is_idempotent_and_does_not_republish(self):
        first = self.run_applier()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.calls.write_text("")
        second = self.run_applier()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), self.expected_name)
        calls = self.calls.read_text()
        self.assertNotIn("ssm put-parameter", calls)
        self.assertIn(f"already names {self.expected_name}", second.stderr)
        self.assertIn(f"applied ConfigMap adp-gateway-agents {self.expected_name}", calls)

    def test_deployment_json_override_and_invalid_inputs(self):
        result = self.run_applier("--deployment-json", str(self.baseline))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("kubectl get deployment", self.calls.read_text())
        bad = subprocess.run(["bash", str(self.script), "--env", "Dev", "--region", "us-east-1"],
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(bad.returncode, 2)
        bad = subprocess.run(["bash", str(self.script), "--env", "dev", "--region", "nowhere"],
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(bad.returncode, 2)

    def test_mutable_gateway_image_is_refused_before_anything_is_applied(self):
        broken = json.loads(self.baseline.read_text())
        broken["spec"]["template"]["spec"]["containers"][0]["image"] = "gateway:latest"
        self.baseline.write_text(json.dumps(broken))
        result = self.run_applier()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable digest", result.stderr)
        calls = self.calls.read_text()
        self.assertNotIn("applied", calls)
        self.assertNotIn("put-parameter", calls)
        self.assertEqual(list((self.tmp / "work").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
