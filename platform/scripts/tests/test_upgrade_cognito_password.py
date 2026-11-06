"""Execute Cognito bootstrap commands with passwords that look like CLI options."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class CognitoPasswordTests(unittest.TestCase):
    def test_generated_passwords_remain_one_literal_argument_for_both_users(self):
        source = (ROOT / 'modules/gateway/infra/modules/cognito/main.tf').read_text()
        commands = [json.loads(value) for value in re.findall(
            r'command\s*=\s*("aws cognito-idp admin-set-user-password(?:\\.|[^"\\])*")', source)]
        self.assertEqual(len(commands), 2, 'Exercise both user and administrator bootstrap')
        parser = '''#!/usr/bin/env python3
import argparse, json, os, sys
assert sys.argv[1:3] == ['cognito-idp', 'admin-set-user-password']
p = argparse.ArgumentParser()
for name in ('user-pool-id', 'username', 'password', 'region'):
    p.add_argument('--' + name, required=True)
p.add_argument('--permanent', action='store_true')
a = p.parse_args(sys.argv[3:])
with open(os.environ['CAPTURE'], 'w') as f:
    json.dump(vars(a), f)
'''
        for command in commands:
            for password in ('Normal9!password', '-Leading9!password', '--permanent',
                             'with space9!password', 'quotes"single\'9!', '$(exit 19)`exit 20`$HOME9!'):
                with self.subTest(command=command, password=password), tempfile.TemporaryDirectory() as tmp:
                    aws = Path(tmp) / 'aws'
                    aws.write_text(parser)
                    aws.chmod(0o755)
                    capture = Path(tmp) / 'args.json'
                    env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                               CAPTURE=str(capture), POOL_ID='us-east-1_fixture',
                               USERNAME='fixture@example.test', REGION='us-east-1', PASSWORD=password)
                    result = subprocess.run(['/bin/sh', '-c', command], env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(capture.read_text()), {
                        'user_pool_id': 'us-east-1_fixture', 'username': 'fixture@example.test',
                        'password': password, 'region': 'us-east-1', 'permanent': True})
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
