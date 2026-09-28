"""Exercise the deployment resolver with a local ECR stub, never live AWS."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "resolve-deepwiki-image.sh"
DIGEST = "sha256:" + "a" * 64


class ResolverTests(unittest.TestCase):
    def resolve(self, selector, digest=DIGEST, fail=False):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            aws = root / "aws"
            aws.write_text(
                "#!/usr/bin/env python3\nimport json,os,pathlib,sys\npathlib.Path(os.environ['TEST_CALL']).write_text(json.dumps(sys.argv[1:]))\nprint(os.environ['TEST_DIGEST'])\nsys.exit(int(os.environ['TEST_FAIL']))\n"
            )
            aws.chmod(0o755)
            env = dict(
                os.environ,
                PATH=work + os.pathsep + os.environ["PATH"],
                ENVIRONMENT="dev",
                AWS_REGION="us-east-1",
                ECR_REGISTRY="123456789012.dkr.ecr.us-east-1.amazonaws.com",
                TEST_CALL=str(root / "call.json"),
                TEST_DIGEST=digest,
                TEST_FAIL=str(int(fail)),
            )
            result = subprocess.run(
                ["bash", str(SCRIPT), selector],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            call = (
                json.loads((root / "call.json").read_text())
                if (root / "call.json").exists()
                else None
            )
            return result, call

    def test_empty_defaults_to_latest_runtime_tag(self):
        result, call = self.resolve("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imageTag=latest", call)
        self.assertEqual(
            result.stdout.strip(),
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@" + DIGEST,
        )

    def test_source_tag_resolves_to_immutable_digest(self):
        result, call = self.resolve("b" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imageTag=" + "b" * 40, call)
        self.assertTrue(result.stdout.strip().endswith("@" + DIGEST))

    def test_digest_uses_exact_ecr_lookup(self):
        result, call = self.resolve(DIGEST)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imageDigest=" + DIGEST, call)

    def test_invalid_selector_is_rejected_before_aws(self):
        for selector in ["sha256:bad", "repo:tag", "$(echo unsafe)", "-option", "a" * 129]:
            with self.subTest(selector=selector):
                result, call = self.resolve(selector)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(call)

    def test_missing_or_failed_lookup_never_falls_back(self):
        for digest, fail in [("None", False), (DIGEST, True)]:
            with self.subTest(digest=digest, fail=fail):
                result, _ = self.resolve("selected", digest=digest, fail=fail)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
