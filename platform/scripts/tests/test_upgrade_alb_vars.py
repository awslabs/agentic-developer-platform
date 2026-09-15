"""Exercise upgrade ALB discovery without contacting AWS or applying Terraform."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]


class UpgradeAlbTests(unittest.TestCase):
    def test_internal_plane_is_preserved_when_cached(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index('    _ALB_ARN=$(aws ssm')
        end = source.index('\n  else\n    terraform apply', start)
        block = source[start:end]
        prefix = '''set -euo pipefail
aws() {
  while [ "$1" != --name ]; do shift; done
  case "$2" in
    */internal-plane-alb-arn) echo "$PLANE_ARN" ;;
    */internal-plane-alb-dns) echo "$PLANE_DNS" ;;
    */internal-plane-alb-security-group-ids) echo '["sg-internal"]' ;;
    */internal-alb-arn) echo edge-arn ;;
    */internal-alb-dns) echo edge.example.test ;;
    */internal-alb-security-group-ids) echo '["sg-edge"]' ;;
    *) return 1 ;;
  esac
}
warn() { :; }
fail() { echo "$*" >&2; exit 1; }
terraform_update_apply() { printf '%s\n' "$@"; }
'''
        for arn, dns, success in (("internal-arn", "internal.example.test", True),
                                  ("", "", True), ("internal-arn", "", False)):
            with self.subTest(arn=arn, dns=dns):
                env = dict(os.environ, ENVIRONMENT="test", AWS_REGION="us-east-1",
                           PLANE_ARN=arn, PLANE_DNS=dns)
                result = subprocess.run(["/bin/bash", "-c", prefix + block],
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if success and arn:
                    self.assertIn("internal_plane_alb_arn=internal-arn", result.stdout)
                    self.assertIn("internal_plane_alb_dns=internal.example.test", result.stdout)
                    self.assertIn('internal_plane_alb_security_group_ids=["sg-internal"]', result.stdout)
                elif success:
                    self.assertNotIn("internal_plane_alb_arn=", result.stdout)


if __name__ == "__main__":
    unittest.main()
