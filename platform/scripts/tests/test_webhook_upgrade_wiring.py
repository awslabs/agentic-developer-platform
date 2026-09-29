"""Exercise deployment wiring defaults and precedence without AWS mutations."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh"


class WebhookUpgradeWiringTests(unittest.TestCase):
    def resolve(self, url, arn, operator=None, live=None):
        source = SCRIPT.read_text()
        start = source.index('      OVERLAY_ARGS=()')
        end = source.index('      terraform_update_apply webhook-ingress', start)
        with tempfile.TemporaryDirectory() as directory:
            overlay = Path(directory) / "operator.json"
            overlay.write_text(json.dumps(operator or {}))
            env = dict(os.environ, UPGRADE_RUN_DIR=directory, GATEWAY_API_URL=url,
                       INTERNAL_API_KEY_ARN=arn, WEBHOOK_UPDATE_VAR_FILE=str(overlay))
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail\n' + source[start:end] +
                 '\nprintf "%s\\n" "${OVERLAY_ARGS[@]}"'],
                env=env, text=True, capture_output=True, check=True,
            )
            merged = {}
            for arg in result.stdout.splitlines():
                merged.update(json.loads(Path(arg.removeprefix("-var-file=")).read_text()))
            # terraform_update_apply loads recovered live context after overlays.
            merged.update(live or {})
            return merged

    def test_missing_legacy_bindings_receive_discovered_defaults(self):
        self.assertEqual(self.resolve("https://gateway/dev", "arn:key"),
                         {"gateway_api_url": "https://gateway/dev", "internal_api_key_arn": "arn:key"})

    def test_operator_and_existing_live_bindings_are_preserved(self):
        self.assertEqual(self.resolve("https://gateway/dev", "arn:key",
                                     {"internal_api_key_arn": "arn:operator"},
                                     {"gateway_api_url": "https://custom/dev"}),
                         {"gateway_api_url": "https://custom/dev", "internal_api_key_arn": "arn:operator"})
        self.assertEqual(self.resolve("", "arn:key", live={"internal_api_key_arn": "arn:live"}),
                         {"internal_api_key_arn": "arn:live"})

    def test_failed_discovery_does_not_replace_configuration_with_empty_values(self):
        self.assertEqual(self.resolve("", "None"), {})
