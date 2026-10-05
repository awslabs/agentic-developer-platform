"""Execute the real publisher and resolver with isolated AWS/Docker doubles."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE = "a" * 40
BASE = "b" * 64
DIGEST = "sha256:" + "c" * 64
REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"
TOOL = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]; tool=Path(sys.argv[0]).name
with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps([tool]+args)+'\n')
mode=os.environ.get('MODE','build')
if tool=='aws':
 if args[:2]==['ecr','get-login-password']: print('fixture-password')
 elif any(x.startswith('imageDigest=') for x in args): print('sha256:'+os.environ['BASE_DIGEST'])
 elif mode=='denied': print('AccessDeniedException',file=sys.stderr);sys.exit(1)
 elif mode=='bad_digest': print('None')
 elif mode=='missing_after_push' and Path(os.environ['PUSHED']).exists(): print('None')
 elif mode in ['reuse','wrong_labels'] or Path(os.environ['PUSHED']).exists(): print('sha256:'+'c'*64)
 else: print('ImageNotFoundException',file=sys.stderr);sys.exit(1)
elif tool=='docker':
 if args[0]=='login': sys.stdin.read()
 if args[0]=='build' and mode=='build_fail': sys.exit(2)
 if args[0]=='run' and mode=='selfcheck_fail': sys.exit(2)
 if args[:2]==['image','inspect']:
  print(json.dumps({'org.opencontainers.image.revision': os.environ['ADP_SOURCE_SHA'],
   'io.adp.image.recipe': 'wrong' if mode=='wrong_labels' else 'direct-browser-v1',
   'io.adp.image.base.digest': 'sha256:'+os.environ['BASE_DIGEST']}))
 if args[0]=='push': Path(os.environ['PUSHED']).touch()
"""


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("aws", "docker"):
            path = self.root / name
            path.write_text(TOOL)
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "ADP_SOURCE_SHA": SOURCE,
            "REGISTRY": REGISTRY,
            "AWS_REGION": "us-east-1",
            "BASE_DIGEST": BASE,
            "WORKER_BASE_IMAGE": f"{REGISTRY}/adp-agent-runtime@sha256:{BASE}",
            "CALLS": str(self.root / "calls"),
            "PUSHED": str(self.root / "pushed"),
        }
        for key in ("IMAGE_TAG", "PUBLISH_LATEST", "MODE"):
            self.env.pop(key, None)

    def publish(self, **overrides):
        self.env.update(overrides)
        result = subprocess.run(
            [
                sys.executable,
                str(
                    ROOT
                    / "modules/domain-apps/cyber/scripts/publish-direct-browser-image.py"
                ),
            ],
            env=self.env,
            text=True,
            capture_output=True,
        )
        path = self.root / "calls"
        calls = (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        return result, calls

    def test_new_recipe_publishes_digest_after_two_selfchecks(self):
        result, calls = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"Verified image: {REGISTRY}/adp-agent-runtime@{DIGEST}", result.stdout
        )
        pushes = [c for c in calls if c[:2] == ["docker", "push"]]
        self.assertEqual(
            pushes,
            [
                [
                    "docker",
                    "push",
                    f"{REGISTRY}/adp-agent-runtime:direct-browser-v1-{SOURCE}-{BASE}",
                ]
            ],
        )
        self.assertEqual(len([c for c in calls if c[:2] == ["docker", "run"]]), 2)
        self.assertTrue(all(c[0] not in ("kubectl", "terraform") for c in calls))

    def test_existing_recipe_is_verified_and_reused_without_push(self):
        result, calls = self.publish(MODE="reuse")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            ["docker", "pull", f"{REGISTRY}/adp-agent-runtime@{DIGEST}"], calls
        )
        self.assertFalse(
            any(c[:2] in (["docker", "build"], ["docker", "push"]) for c in calls)
        )

    def test_recipe_identity_changes_with_base(self):
        changed = "d" * 64
        result, calls = self.publish(
            BASE_DIGEST=changed,
            WORKER_BASE_IMAGE=f"{REGISTRY}/adp-agent-runtime@sha256:{changed}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            [
                "docker",
                "push",
                f"{REGISTRY}/adp-agent-runtime:direct-browser-v1-{SOURCE}-{changed}",
            ],
            calls,
        )

    def test_wrong_recipe_reuse_fails_without_push(self):
        result, calls = self.publish(MODE="wrong_labels")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance", result.stderr)
        self.assertFalse(any(c[:2] == ["docker", "push"] for c in calls))

    def test_generic_sha_and_latest_tags_are_rejected_before_tooling(self):
        for tag in (SOURCE, "latest", "short"):
            with self.subTest(tag=tag):
                result, calls = self.publish(IMAGE_TAG=tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_invalid_inputs_fail_before_tooling(self):
        for values in (
            {"ADP_SOURCE_SHA": "short"},
            {"WORKER_BASE_IMAGE": f"{REGISTRY}/adp-agent-runtime:latest"},
            {"PUBLISH_LATEST": "true"},
            {"REGISTRY": "wrong"},
        ):
            with self.subTest(values=values):
                before = self.env.copy()
                result, calls = self.publish(**values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])
                self.env = before

    def test_registry_denial_and_bad_digest_fail_before_docker(self):
        for mode in ("denied", "bad_digest"):
            with self.subTest(mode=mode):
                result, calls = self.publish(MODE=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[0] == "docker" for c in calls))

    def test_base_digest_mismatch_prevents_docker(self):
        result, calls = self.publish(BASE_DIGEST="e" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == "docker" for c in calls))

    def test_missing_published_digest_does_not_report_success(self):
        result, calls = self.publish(MODE="missing_after_push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Published digest missing", result.stderr)
        self.assertNotIn("Verified image:", result.stdout)
        self.assertTrue(any(c[:2] == ["docker", "push"] for c in calls))

    def test_build_and_selfcheck_failure_prevent_push(self):
        for mode in ("build_fail", "selfcheck_fail"):
            with self.subTest(mode=mode):
                result, calls = self.publish(MODE=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[:2] == ["docker", "push"] for c in calls))


if __name__ == "__main__":
    unittest.main()
