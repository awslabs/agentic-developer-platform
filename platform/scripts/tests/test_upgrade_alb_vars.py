"""Execute real ALB argument construction with cached/absent/error responses."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]


class UpgradeAlbTests(unittest.TestCase):
    def test_internal_plane_is_preserved_and_errors_fail_closed(self):
        prefix = r'''set -euo pipefail
aws() {
  if [ "$AWS_FAIL" = true ]; then return 1; fi
  if [[ "$*" == *internal-plane* ]]; then echo "$PLANE_JSON"; else echo "$EDGE_JSON"; fi
}
fail() { echo "$*" >&2; exit 1; }
source "$HELPER"
gateway_alb_vars
printf '%s\n' "${GATEWAY_ALB_ARGS[@]}"
'''
        def parameters(name):
            return {"Parameters": [{"Name": "/adp/test/gateway/"+name+suffix, "Value": value}
                                   for suffix, value in [("-arn", name+"-arn"), ("-dns", name+".test"),
                                                         ("-security-group-ids", '["sg-test"]')]], "InvalidParameters": []}
        full = parameters("internal-plane-alb")
        partial = parameters("internal-plane-alb")
        partial["Parameters"].pop()
        for plane, aws_fail, success in ((full, False, True), ({"Parameters": []}, False, True),
                                          (partial, False, False), (full, True, False)):
            with self.subTest(plane=plane, aws_fail=aws_fail):
                env = dict(os.environ, ENVIRONMENT="test", AWS_REGION="us-east-1", HELPER=str(ROOT / "platform/scripts/gateway-alb-vars.sh"),
                           PLANE_JSON=json.dumps(plane), EDGE_JSON=json.dumps(parameters("internal-alb")), AWS_FAIL=str(aws_fail).lower())
                result = subprocess.run(["/bin/bash", "-c", prefix], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if success:
                    self.assertIn("enable_vpc_origin=true", result.stdout)
                    self.assertEqual("internal_plane_alb_arn=" in result.stdout, bool(plane["Parameters"]))


if __name__ == "__main__":
    unittest.main()
