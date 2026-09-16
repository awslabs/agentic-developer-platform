"""Render the actual deploy script's Deployment block without AWS/Kubernetes."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]


class DeploymentRenderTests(unittest.TestCase):
    def test_image_and_all_feature_flags_are_rendered(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("  # Render deployment settings as well as the ConfigMap.")
        end = source.index('\n  if [ "$UPDATE_MODE" = true ]; then', start)
        block = source[start:end]
        for enabled in ("false", "true"):
            with self.subTest(enabled=enabled):
                env = dict(os.environ, ENVIRONMENT="test", REGISTRY="example.test",
                           IMAGE_TAG="release-sha", TEST_FLAG=enabled)
                prefix = '''set -euo pipefail
_get_ssm() { echo "$TEST_FLAG"; }
kubectl() { cat; }
'''
                result = subprocess.run(["/bin/bash", "-c", prefix + block],
                                        cwd=ROOT / "modules/gateway", env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotRegex(result.stdout, r'__[A-Z_]+__|REPLACE_WITH_GATEWAY_IMAGE')
                self.assertIn("image: example.test/adp-gateway:release-sha", result.stdout)
                for flag in ("FEATURE_ORCHESTRATION_ENGINE_ENABLED",
                             "FEATURE_AGENT_CONTROL_ENABLED", "FEATURE_NEW_UI_ENABLED"):
                    self.assertRegex(result.stdout, rf'name: {flag}\s+value: "{enabled}"')


if __name__ == "__main__":
    unittest.main()
