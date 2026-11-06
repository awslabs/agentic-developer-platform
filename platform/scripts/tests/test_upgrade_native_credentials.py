"""Keep an approved native signer across source and portable release upgrades."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("native_upgrade_state", ROOT / "platform/scripts/upgrade-state.py")
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)


def signer(**changes):
    attrs = {"customer_master_key_spec": "RSA_3072", "key_usage": "SIGN_VERIFY", **changes}
    return {"mode": "managed", "module": "module.native_credentials", "type": "aws_kms_key", "name": "issuer",
            "instances": [{"index_key": 0, "attributes": attrs}]}


class NativeCredentialUpgradeTests(unittest.TestCase):
    def modes(self):
        return ("false", "true")

    def test_recorded_native_choice_survives_both_upgrade_modes(self):
        for mode in self.modes():
            for enabled in (False, True):
                with self.subTest(mode=mode, enabled=enabled), patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                    fixture = {"outputs": {"release_configuration": {"value": {"native_credentials_enabled": enabled}}}}
                    self.assertIs(state.release_settings(fixture, "gateway")["native_credentials_enabled"], enabled)

    def test_targeted_enablement_without_updated_root_outputs_retains_owned_key(self):
        # Terraform -target omits unrelated root outputs. This was observed in
        # the first native owner plan, so no recorded flag is available yet.
        for mode in self.modes():
            for outputs in ({}, {"release_configuration": {"value": {"cost_center": "engineering"}}},
                            {"release_configuration": {"value": {"native_credentials_enabled": False}}}):
                with self.subTest(mode=mode, outputs=outputs), patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                    fixture = {"outputs": outputs, "resources": [signer()]}
                    self.assertIs(state.release_settings(fixture, "gateway")["native_credentials_enabled"], True)

    def test_fresh_or_unrelated_key_does_not_enable_native_credentials(self):
        unrelated = signer()
        unrelated["module"] = "module.other"
        for mode in self.modes():
            for fixture in ({}, {"resources": [unrelated]}):
                with self.subTest(mode=mode, fixture=fixture), patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                    self.assertNotIn("native_credentials_enabled", state.release_settings(fixture, "gateway"))

    def test_stale_disabled_output_cannot_remove_an_owned_signer(self):
        fixture = {"outputs": {"release_configuration": {"value": {"native_credentials_enabled": False}}}, "resources": [signer()]}
        for mode in self.modes():
            with self.subTest(mode=mode), patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                self.assertIs(state.release_settings(fixture, "gateway")["native_credentials_enabled"], True)

    def test_invalid_recorded_flag_is_not_coerced_into_enablement(self):
        for mode in self.modes():
            for value in ("true", "false", 0, 1, None):
                with self.subTest(mode=mode, value=value), patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                    fixture = {"outputs": {"release_configuration": {"value": {"native_credentials_enabled": value}}}}
                    with self.assertRaisesRegex(ValueError, "native credential"):
                        state.release_settings(fixture, "gateway")

    def test_unexpected_or_ambiguous_native_key_requires_review(self):
        for mode in self.modes():
            for keys in ([signer(key_usage="ENCRYPT_DECRYPT")], [signer(customer_master_key_spec="RSA_2048")], [signer(), signer()]):
                with self.subTest(mode=mode, keys=keys), patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                    with self.assertRaisesRegex(ValueError, "native credential"):
                        state.release_settings({"resources": keys}, "gateway")

    def reader(self, document):
        return subprocess.run([sys.executable, str(ROOT / "platform/scripts/native_credential_inputs.py")],
                              input=json.dumps(document), capture_output=True, text=True)

    def test_owner_workflow_reader_retains_targeted_key_without_exposing_state(self):
        document = {"format_version": "1.0", "values": {"root_module": {"child_modules": [{
            "address": "module.native_credentials", "resources": [{
                "address": "module.native_credentials.aws_kms_key.issuer[0]", "mode": "managed",
                "values": signer()["instances"][0]["attributes"]}]}]},
            "outputs": {"unrelated_secret": {"value": "must-never-be-printed", "sensitive": True}}}}
        result = self.reader(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"native_credentials_enabled": True})
        self.assertNotIn("must-never-be-printed", result.stdout + result.stderr)

    def test_owner_workflow_reader_accepts_empty_backend_and_retained_input(self):
        for document, expected in (
            ({"format_version": "1.0"}, {}),
            ({"format_version": "1.0", "values": {"outputs": {"release_configuration": {
                "value": {"native_credentials_enabled": True}, "sensitive": True}}}}, {"native_credentials_enabled": True}),
        ):
            with self.subTest(document=document):
                result = self.reader(document)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_owner_workflow_reader_refuses_malformed_or_plan_input_without_echo(self):
        for document in ({}, [], {"format_version": "future", "value": "must-never-be-printed"},
                         {"format_version": "1.0", "planned_values": {}},
                         {"format_version": "1.0", "values": {"outputs": {"release_configuration": {"value": {
                             "native_credentials_enabled": "must-never-be-printed"}}}}}):
            with self.subTest(document=document):
                result = self.reader(document)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("must-never-be-printed", result.stderr)

    def test_actual_owner_workflow_steps_keep_state_private_and_fail_closed(self):
        for name in ("gateway-infra-plan.yml", "gateway-infra-apply.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            step = text.split("      - name: Retain native credential owner input\n", 1)[1].split("\n      - name:", 1)[0]
            script = textwrap.dedent(step.split("        run: |\n", 1)[1])
            prefix = 'terraform() { [ "$STATE_READ_FAIL" = false ] || return 1; printf "%s\\n" "$OWNER_STATE"; }\n'
            document = {"format_version": "1.0", "values": {"outputs": {
                "release_configuration": {"value": {"native_credentials_enabled": True}},
                "secret": {"value": "must-never-be-printed", "sensitive": True}}}}
            for fail in (False, True):
                with self.subTest(workflow=name, state_read_fail=fail), tempfile.TemporaryDirectory() as temporary:
                    journal = Path(temporary) / "environment"
                    env = dict(os.environ, RUNNER_TEMP=temporary, GITHUB_ENV=str(journal), OWNER_STATE=json.dumps(document),
                               STATE_READ_FAIL=str(fail).lower())
                    result = subprocess.run(["bash", "-c", prefix + script], cwd=ROOT / "modules/gateway/infra",
                                            env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, not fail, result.stderr)
                    self.assertNotIn("must-never-be-printed", result.stdout + result.stderr)
                    self.assertEqual(journal.exists(), not fail)
                    if not fail:
                        overlay = Path(journal.read_text().strip().split("=", 1)[1])
                        self.assertEqual(json.loads(overlay.read_text()), {"native_credentials_enabled": True})
                        self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
