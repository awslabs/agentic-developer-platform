"""Exercise fresh, repeated and failed IAM bootstrap without cloud access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[3] / 'modules/gateway/infra/modules/rds-bootstrap/bootstrap.sh'


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_requires_verified_iam_and_reuses_existing_grant(self):
        for mode in ('fresh', 'existing', 'denied'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mocks = {
                    'dnf': '#!/bin/bash\nexit 0\n',
                    'aws': '''#!/bin/bash
if [ "$1" = rds ]; then echo iam-token; else
  echo secret >> "$CALLS"
  echo '{"username":"bgadmin","password":"fixture-password"}'
fi
''',
                    'psql': '''#!/bin/bash
if [ "$PGPASSWORD" = iam-token ]; then
  echo iam >> "$CALLS"
  [ "$MODE" != denied ] && { [ "$MODE" = existing ] || [ -f "$GRANTED" ]; }
else
  echo grant >> "$CALLS"
  cat >/dev/null
  touch "$GRANTED"
fi
''',
                }
                for name, content in mocks.items():
                    path = root / name
                    path.write_text(content)
                    path.chmod(0o755)
                calls = root / 'calls'
                result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True,
                                        env=dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                                                 MODE=mode, CALLS=str(calls), GRANTED=str(root / 'granted'),
                                                 DB_HOST='db.invalid', DB_USER='bgadmin', DB_NAME='gateway',
                                                 AWS_REGION='us-east-1', SECRET_ID='fixture'))
                self.assertEqual(result.returncode == 0, mode != 'denied', result.stderr)
                expected = ['iam'] if mode == 'existing' else ['iam', 'secret', 'grant', 'iam']
                self.assertEqual(calls.read_text().splitlines(), expected)
                self.assertNotIn('fixture-password', result.stdout + result.stderr)
                if mode == 'denied':
                    self.assertNotIn('bootstrap complete', result.stdout)


if __name__ == '__main__':
    unittest.main()
