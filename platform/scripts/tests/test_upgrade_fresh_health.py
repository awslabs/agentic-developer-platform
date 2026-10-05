"""Fresh installs must verify the public API before marking verification complete."""
import os
from pathlib import Path
import subprocess
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
aws() { echo example.cloudfront.net; }
curl() { printf '%s' "$RESPONSE"; return "$HTTP_EXIT"; }
'''
        for body, http_exit, success in (
            ('{"status":"healthy"}', '0', True),
            ('{"status":"unhealthy"}', '0', False),
            ('<html>frontend fallback</html>', '0', False),
            ('{"status":"healthy"}', '22', False),
        ):
            with self.subTest(body=body, http_exit=http_exit):
                env = dict(os.environ, UPDATE_MODE='false', DEPLOY_GATEWAY='true',
                           CI_MODE='true', ENVIRONMENT='dev', RESPONSE=body, HTTP_EXIT=http_exit)
                result = subprocess.run(['/bin/bash', '-c', shell + block], env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual('VERIFIED' in result.stdout, success)


if __name__ == '__main__':
    unittest.main()
