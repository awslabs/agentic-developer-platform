"""A transient empty ELB SG response must not remove VPC Link access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "alb-security-groups.sh"


class AlbSecurityGroupsTests(unittest.TestCase):
    def read(self, responses):
        with tempfile.TemporaryDirectory() as directory:
            response_file = Path(directory) / "responses.json"
            count_file = Path(directory) / "count"
            response_file.write_text(json.dumps(responses))
            script = r'''
set -euo pipefail
aws() {
  local count=0
  [ ! -f "$COUNT_FILE" ] || count=$(cat "$COUNT_FILE")
  echo $((count + 1)) > "$COUNT_FILE"
  python3 - "$RESPONSES_FILE" "$count" <<'PY'
import json, sys
responses=json.load(open(sys.argv[1]))
print(responses[min(int(sys.argv[2]), len(responses)-1)])
PY
}
sleep() { :; }
source "$HELPER"
read_alb_security_groups arn:test
'''
            env = dict(os.environ, AWS_REGION="us-east-1", HELPER=str(HELPER),
                       RESPONSES_FILE=str(response_file), COUNT_FILE=str(count_file))
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            return result, int(count_file.read_text())

    def test_retries_empty_response_and_returns_valid_groups(self):
        result, calls = self.read(["[]", '["sg-abc123","sg-def456"]'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["sg-abc123", "sg-def456"])
        self.assertEqual(calls, 2)

    def test_fails_when_groups_remain_empty(self):
        result, calls = self.read(["[]"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(calls, 5)

    def test_rejects_malformed_group_ids(self):
        result, calls = self.read(['["not-a-security-group"]'])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, 5)


if __name__ == "__main__":
    unittest.main()
