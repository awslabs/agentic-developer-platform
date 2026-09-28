"""Execute the production installer gates with controlled network payloads."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
TRUSTED = b'#!/bin/bash\necho reviewed fixture\n'


def gate(name):
    if name == 'beads':
        source = (ROOT / 'modules/agent-factory/actions/setup-beads/action.yml').read_text()
        return textwrap.dedent(source[source.index('        BEADS_INSTALLER_SHA256='):source.index('        # Add to PATH')])
    if name == 'gitlab':
        source = (ROOT / 'modules/source-control/gitlab/infra/user_data.sh').read_text()
        return source[source.index('GITLAB_REPO_SHA256='):source.index('# Install GitLab CE\n')]
    source = (ROOT / 'platform/scripts/install-prereqs.sh').read_text()
    return source[source.index('    HELM_INSTALLER_SHA256='):source.index('    rm -f "$HELM_INSTALLER"') + len('    rm -f "$HELM_INSTALLER"')]


class InstallerExecutionBoundary(unittest.TestCase):
    def run_gate(self, name, payload, network_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / 'payload').write_bytes(payload)
            bin_dir = temp / 'bin'
            bin_dir.mkdir()
            # Mock only network and execution. Use the real checksum utility and
            # production conditionals, including the download failure branch.
            scripts = {
                'curl': '''#!/bin/bash
if [ "$NETWORK_FAILURE" = 1 ]; then exit 22; fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then cp "$FIXTURE_DIR/payload" "$2"; exit 0; fi
  shift
done
exit 2
''',
                'bash': '#!/bin/sh\nprintf executed > "$FIXTURE_DIR/executed"\n',
                'mktemp': '#!/bin/sh\n/usr/bin/mktemp "$FIXTURE_DIR/download.XXXXXX"\n',
            }
            for command, content in scripts.items():
                path = bin_dir / command
                path.write_text(content)
                path.chmod(0o755)
            script = re.sub(r'(?m)^(\s*\w+_SHA256=)"[a-f0-9]{64}"',
                            lambda match: match[1] + '"' + hashlib.sha256(TRUSTED).hexdigest() + '"', gate(name))
            env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'],
                       FIXTURE_DIR=str(temp), NETWORK_FAILURE=str(int(network_failure)))
            result = subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\nok() { :; }; warn() { :; };\n' + script],
                                    env=env, capture_output=True, text=True)
            return result, (temp / 'executed').exists(), list(temp.glob('download.*'))

    def test_reviewed_content_executes_and_cleans_up(self):
        for name in ('beads', 'gitlab', 'helm'):
            with self.subTest(installer=name):
                result, executed, leftovers = self.run_gate(name, TRUSTED)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(executed)
                self.assertFalse(leftovers)

    def test_valid_shell_with_changed_bytes_never_executes(self):
        for name in ('beads', 'gitlab', 'helm'):
            for payload in (b'#!/bin/bash\necho compromised\n', b''):
                with self.subTest(installer=name, empty=not payload):
                    _, executed, leftovers = self.run_gate(name, payload)
                    self.assertFalse(executed)
                    self.assertFalse(leftovers)

    def test_failed_download_never_executes(self):
        for name in ('beads', 'gitlab', 'helm'):
            with self.subTest(installer=name):
                _, executed, leftovers = self.run_gate(name, TRUSTED, network_failure=True)
                self.assertFalse(executed)
                self.assertFalse(leftovers)


if __name__ == '__main__':
    unittest.main()
