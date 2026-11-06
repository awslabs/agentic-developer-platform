"""Fresh installs must verify the public API before marking verification complete."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class FreshHealthGateTests(unittest.TestCase):
    def test_fresh_install_requires_successful_healthy_json(self):
        source = (ROOT / 'platform/scripts/deploy-all.sh').read_text()
        start = source.index('if deploy_phase_begin verify; then')
        block = source[start:source.index('# Summary', start)]
        shell = r'''set -euo pipefail
deploy_phase_begin() { return 0; }
deploy_phase_complete() { echo VERIFIED; }
step() { :; }
fail() { echo "$*" >&2; exit 1; }
capacity_lock() { :; }
python3() {
  if [ "$1" = "$SCRIPT_DIR/render-gateway-capacity.py" ]; then return 0; fi
  command python3 "$@"
}
kubectl() { :; }
wait_for_gateway_rollout() { return 0; }
aws() { echo example.cloudfront.net; }
curl() { printf '%s' "$RESPONSE"; return "$HTTP_EXIT"; }
'''
        for body, http_exit, success in (
            ('{"status":"healthy"}', '0', True),
            ('{"status":"unhealthy"}', '0', False),
            ('<html>frontend fallback</html>', '0', False),
            ('{"status":"healthy"}', '22', False),
        ):
            with self.subTest(body=body, http_exit=http_exit), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, UPDATE_MODE='false', DEPLOY_GATEWAY='true',
                           CI_MODE='true', ROOT_DIR=str(ROOT), ENVIRONMENT='dev', RESPONSE=body, HTTP_EXIT=http_exit,
                           SCRIPT_DIR=str(ROOT / 'platform/scripts'), CAPACITY_BUNDLE=f'{directory}/bundle',
                           CAPACITY_LOCK_DIR=f'{directory}/lock')
                result = subprocess.run(['/bin/bash', '-c', shell + block], env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual('VERIFIED' in result.stdout, success)


if __name__ == '__main__':
    unittest.main()
