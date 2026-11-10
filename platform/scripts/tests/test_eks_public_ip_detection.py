"""EKS public-endpoint allowlisting must refuse hosts whose egress address rotates.

A single /32 from one lookup locks Terraform and kubectl out of the EKS API when
later connections leave from a different address of an egress pool.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "platform/scripts/deploy-all.sh"
BASH = shutil.which("bash")
HARNESS = """set -euo pipefail
ok() { echo "OK: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
"""
CHECKIP = "https://checkip.amazonaws.com"


def detection_block():
    source = SCRIPT.read_text()
    start = source.index("detect_public_ips() {")
    marker = 'ok "EKS access: preserving existing configuration during upgrades"\nfi\n'
    return source[start : source.index(marker, start) + len(marker)]


class EksPublicIpDetectionTests(unittest.TestCase):
    def run_block(self, responses, update_mode="false", preset=None):
        """responses maps URL -> list of bodies returned on successive calls ("" = failure)."""
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            state = Path(directory) / "responses.json"
            state.write_text(json.dumps(responses))
            calls = Path(directory) / "calls"
            curl = bin_dir / "curl"
            curl.write_text(
                f"""#!/usr/bin/env python3
import json, sys
url = sys.argv[-1]
with open({str(calls)!r}, "a") as f:
    f.write(url + "\\n")
data = json.load(open({str(state)!r}))
queue = data.get(url, [])
body = queue.pop(0) if queue else ""
json.dump(data, open({str(state)!r}, "w"))
if not body:
    sys.exit(7)
print(body)
"""
            )
            curl.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                UPDATE_MODE=update_mode,
            )
            env.pop("TF_VAR_eks_public_access_cidrs", None)
            if preset is not None:
                env["TF_VAR_eks_public_access_cidrs"] = preset
            script = (
                HARNESS
                + detection_block()
                + 'echo "CIDRS=${TF_VAR_eks_public_access_cidrs:-}"'
            )
            result = subprocess.run(
                [BASH, "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            made = calls.read_text().split() if calls.exists() else []
            return result, made

    def test_stable_address_is_allowlisted(self):
        result, calls = self.run_block({CHECKIP: ["192.0.2.10"] * 3})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CIDRS=["192.0.2.10/32"]', result.stdout)
        self.assertEqual(calls, [CHECKIP] * 3)

    def test_rotating_egress_is_refused_with_guidance(self):
        result, _ = self.run_block(
            {CHECKIP: ["192.0.2.10", "192.0.2.77", "192.0.2.10"]}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Public IP lookups disagree (192.0.2.10, 192.0.2.77)", result.stderr
        )
        self.assertIn("TF_VAR_eks_public_access_cidrs", result.stderr)
        self.assertNotIn("CIDRS=", result.stdout)

    def test_one_failed_sample_does_not_hide_a_stable_address(self):
        result, _ = self.run_block({CHECKIP: ["", "192.0.2.10", "192.0.2.10"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CIDRS=["192.0.2.10/32"]', result.stdout)

    def test_fallback_lookup_is_used_only_when_checkip_is_unavailable(self):
        result, calls = self.run_block({"https://api.ipify.org": ["198.51.100.5"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CIDRS=["198.51.100.5/32"]', result.stdout)
        self.assertNotIn("https://ifconfig.me", calls)

    def test_non_ipv4_answers_are_ignored(self):
        result, _ = self.run_block(
            {
                CHECKIP: ["<html>error</html>", "", ""],
                "https://api.ipify.org": [""],
                "https://ifconfig.me": ["2001:db8::1"],
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not detect your public IP", result.stderr)

    def test_operator_cidrs_are_respected_without_lookups(self):
        result, calls = self.run_block({}, preset='["203.0.113.0/24"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CIDRS=["203.0.113.0/24"]', result.stdout)
        self.assertEqual(calls, [])

    def test_updates_do_not_detect(self):
        result, calls = self.run_block({}, update_mode="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preserving existing configuration", result.stdout)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
