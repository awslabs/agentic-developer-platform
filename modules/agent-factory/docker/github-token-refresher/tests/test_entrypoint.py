"""Run the shipped Bash entrypoint with disposable AWS/GitHub providers.

No real credentials/config are inherited. curl/aws are fixture executables;
OpenSSL signs with a freshly generated test-only key. Run with unittest discovery.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(
    os.environ.get("TOKEN_REFRESHER_SCRIPT", Path(__file__).parents[1] / "github-app-token.sh")
)

PROVIDER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
scenario = os.environ["FIXTURE_SCENARIO"]
with (root / "calls").open("a") as f:
    f.write(name + " " + (args[-1] if name == "curl" else "") + "\n")
if name == "aws":
    secret = args[args.index("--secret-id") + 1]
    if (scenario == "secret-failure" or (scenario == "refresh-secret-failure" and (root / "sleeps").exists())) and secret.endswith("app-id"):
        sys.exit(1)
    print((root / "key.pem").read_text() if secret.endswith("app-key") else
          "fixture_gateway_key" if secret.endswith("api-key") else "12345")
elif name == "sleep":
    count = root / "sleeps"
    if count.exists():
        sys.exit(42)
    count.write_text("1")
elif name == "curl":
    url = args[-1]
    if url.endswith("/app/installations"):
        if scenario == "installation-failure" and (root / "sleeps").exists():
            sys.exit(22)
        installations = [{"id": 99, "account": {"login": "other-tenant"}}]
        if scenario != "wrong-owner":
            installations.append({"id": 42, "account": {"login": "fixture-org"}})
        if scenario == "duplicate-owner":
            installations.append({"id": 43, "account": {"login": "FIXTURE-ORG"}})
        if scenario == "invalid-installation":
            installations[-1]["id"] = "../99"
        print(json.dumps(installations))
    else:
        if url != "https://api.github.invalid/app/installations/42/access_tokens":
            sys.exit(23)
        if scenario == "refresh-failure" and (root / "sleeps").exists():
            sys.exit(22)
        if scenario == "refresh-malformed" and (root / "sleeps").exists():
            print('{"token":null}')
        elif scenario == "empty-token":
            print('{"token":""}')
        elif scenario == "null-token":
            print('{"token":null}')
        elif scenario == "newline-token":
            print(json.dumps({"token": "fixture_token\nmalformed"}))
        elif scenario == "bad-json":
            print("not json")
        else:
            print(json.dumps({"token": "fixture_installation_token", "expires_at": "2099-01-01T00:00:00Z"}))
"""


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("aws", "curl", "sleep"):
            path = self.bin / name
            path.write_text(PROVIDER)
            path.chmod(0o755)
        subprocess.run(
            ["openssl", "genrsa", "-out", str(self.root / "key.pem"), "2048"],
            check=True,
            capture_output=True,
        )
        self.token = self.root / "shared" / "token"
        self.token.parent.mkdir()

    def run_entrypoint(self, scenario="success", sidecar=False, owner="FiXtUrE-OrG"):
        env = {
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "FIXTURE_ROOT": str(self.root),
            "FIXTURE_SCENARIO": scenario,
            "GITHUB_APP_OWNER": owner,
            "GITHUB_API_URL": "https://api.github.invalid",
            "GITHUB_TOKEN_PATH": str(self.token),
            "SECRETS_DIR": str(self.root / "secrets"),
            "SIDECAR_MODE": str(sidecar).lower(),
            "GITHUB_TOKEN_REFRESH_INTERVAL": "0",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=20
        )

    def test_init_writes_only_token_with_private_permissions(self):
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.token.read_text(), "fixture_installation_token")
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        key = self.root / "secrets" / "gateway-api-key"
        self.assertEqual(key.read_text(), "fixture_gateway_key")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fixture_installation_token", result.stdout + result.stderr)
        self.assertNotIn("fixture_gateway_key", result.stdout + result.stderr)

    def test_missing_owner_makes_no_provider_calls(self):
        result = self.run_entrypoint(owner="")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "calls").exists())

    def test_unmatched_owner_never_mints_token(self):
        result = self.run_entrypoint("wrong-owner")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("access_tokens", (self.root / "calls").read_text())
        self.assertFalse(self.token.exists())

    def test_ambiguous_or_invalid_installation_never_mints_token(self):
        for scenario in ("duplicate-owner", "invalid-installation"):
            with self.subTest(scenario=scenario):
                (self.root / "calls").unlink(missing_ok=True)
                result = self.run_entrypoint(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("access_tokens", (self.root / "calls").read_text())
                self.assertFalse(self.token.exists())

    def test_malformed_response_preserves_existing_token(self):
        for scenario in ("empty-token", "null-token", "newline-token", "bad-json"):
            with self.subTest(scenario=scenario):
                self.token.write_text("previous_valid_token")
                result = self.run_entrypoint(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.token.read_text(), "previous_valid_token")

    def test_sidecar_provider_failures_preserve_last_good_token(self):
        for scenario in (
            "refresh-failure",
            "installation-failure",
            "refresh-secret-failure",
            "refresh-malformed",
        ):
            with self.subTest(scenario=scenario):
                (self.root / "sleeps").unlink(missing_ok=True)
                result = self.run_entrypoint(scenario, sidecar=True)
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertEqual(self.token.read_text(), "fixture_installation_token")
                self.assertIn("Token refresh failed", result.stdout + result.stderr)

    def test_failed_atomic_replace_preserves_token_and_cleans_temporary_file(self):
        self.token.write_text("previous_valid_token")
        # Only fail token replacement; gateway-secret initialization still works.
        replacement = self.bin / "mv"
        replacement.write_text(
            '#!/bin/sh\ncase "$*" in *shared/token*) exit 1;; esac\nexec /bin/mv "$@"\n'
        )
        replacement.chmod(0o755)
        result = self.run_entrypoint()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.token.read_text(), "previous_valid_token")
        self.assertEqual(list(self.token.parent.iterdir()), [self.token])

    def test_secret_failure_stops_before_github(self):
        result = self.run_entrypoint("secret-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("curl", (self.root / "calls").read_text())
        self.assertFalse(self.token.exists())

    def test_signing_failure_cleans_private_key_and_stops(self):
        (self.root / "key.pem").write_text("invalid fixture key")
        result = self.run_entrypoint()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("curl", (self.root / "calls").read_text())
        self.assertEqual(list(self.root.glob("tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
