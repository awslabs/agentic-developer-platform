"""Execute the production publisher/deployer with isolated AWS, Docker and TF tools."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
SHA = "b" * 40


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = self.root / "repo/modules/research/gbrain"
        (self.module / "scripts").mkdir(parents=True)
        (self.module / "terraform").mkdir()
        for script in ("deploy-image.sh", "publish-image.sh"):
            shutil.copy(MODULE / "scripts" / script, self.module / "scripts" / script)
        build = self.root / "repo/platform/scripts/codebuild-run.sh"
        build.parent.mkdir(parents=True)
        build.write_text('''#!/bin/bash
printf 'codebuild %s %s\\n' "$SOURCE_SHA" "$ADP_RELEASE_BUILD" >> "$LOG"
exit "${BUILD_STATUS:-0}"
''')
        self.log = self.root / "calls"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = '''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['LOG'],'a') as f: f.write(json.dumps([name,*args])+'\\n')
if name=='aws':
 if args[:2]==['sts','get-caller-identity']: print('111122223333')
 elif args[:2]==['s3api','head-object']:
  if os.environ.get('STATE_EXISTS')=='true': print('{}')
  else:
   print('AccessDenied' if os.environ.get('STATE_ERROR') else 'NoSuchKey',file=sys.stderr);sys.exit(1)
 elif args[:2]==['ecs','describe-services']:
  print('gbrain' if os.environ.get('SERVICE_EXISTS')=='true' else '')
 elif args[:2]==['ecr','get-login-password']: print('stub')
 elif args[:2]==['ecr','describe-images']:
  if os.environ.get('REGISTRY_ERROR'):
   print('AccessDeniedException',file=sys.stderr);sys.exit(1)
  if os.environ.get('MISSING_IMAGE')=='true' and not Path(os.environ['PUSHED']).exists():
   print('ImageNotFoundException',file=sys.stderr);sys.exit(1)
  print(os.environ.get('RESULT_DIGEST','sha256:'+'a'*64))
elif name=='docker':
 if args[0]=='login': sys.stdin.read()
 if args[0]=='build': sys.exit(int(os.environ.get('BUILD_STATUS','0')))
 if args[0]=='push':
  if os.environ.get('PUSH_STATUS'): sys.exit(int(os.environ['PUSH_STATUS']))
  Path(os.environ['PUSHED']).touch()
elif name=='terraform':
 if args[0]=='output': print('gbrain-build')
'''
        for name in ("aws", "docker", "terraform"):
            p = self.bin / name
            p.write_text(stub)
            p.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "LOG": str(self.log),
                    "PUSHED": str(self.root / "pushed"), "ADP_SOURCE_SHA": SHA,
                    "SOURCE_SHA": SHA, "REGISTRY": "example.invalid", "AWS_REGION": "us-east-1"}
        for name in ("GBRAIN_IMAGE_DIGEST", "BUILD_STATUS", "PUSH_STATUS", "MISSING_IMAGE", "REGISTRY_ERROR", "RESULT_DIGEST", "GBRAIN_RUNTIME_TFVARS", "GBRAIN_RUNTIME_TFVARS_SHA256", "STATE_EXISTS", "STATE_ERROR", "SERVICE_EXISTS"):
            self.env.pop(name, None)

    def run_script(self, script, **env):
        result = subprocess.run(["bash", str(self.module / "scripts" / script)],
                                env={**self.env, **env}, capture_output=True, text=True, timeout=10)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def test_first_publication(self):
        result, calls = self.run_script("publish-image.sh", MISSING_IMAGE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum('"push"' in c for c in calls), 1)
        self.assertIn(DIGEST, result.stdout)

    def test_immutable_retry_reuses_existing_digest(self):
        result, calls = self.run_script("publish-image.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('"docker"' in c for c in calls))
        self.assertIn("Reusing", result.stdout)

    def test_registry_error_does_not_rebuild(self):
        result, calls = self.run_script("publish-image.sh", REGISTRY_ERROR="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('"docker"' in c for c in calls))

    def test_failed_build_never_pushes(self):
        result, calls = self.run_script("publish-image.sh", MISSING_IMAGE="true", BUILD_STATUS="4")
        self.assertEqual(result.returncode, 4)
        self.assertFalse(any('"push"' in c for c in calls))

    def test_failed_push_is_failure(self):
        result, calls = self.run_script("publish-image.sh", MISSING_IMAGE="true", PUSH_STATUS="5")
        self.assertEqual(result.returncode, 5)
        self.assertNotIn("Published", result.stdout)

    def test_publisher_rejects_missing_digest(self):
        result, _ = self.run_script("publish-image.sh", RESULT_DIGEST="None")
        self.assertNotEqual(result.returncode, 0)

    def test_publisher_rejects_untrusted_source_tag(self):
        result, calls = self.run_script("publish-image.sh", ADP_SOURCE_SHA="latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_bootstrap_only_build_resources_before_digest_activation(self):
        result, calls = self.run_script("deploy-image.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        applies = [json.loads(c) for c in calls if c.startswith('["terraform", "apply"')]
        self.assertEqual(len(applies), 2)
        self.assertIn("-target=module.storage", applies[0])
        self.assertIn("-target=module.build", applies[0])
        self.assertIn("-var=container_image_digest=" + DIGEST, applies[1])
        self.assertFalse(any(a.startswith("-target") for a in applies[1]))
        self.assertIn("codebuild " + SHA + " true", calls)
        verify = next(i for i,c in enumerate(calls) if "imageDigest=" + DIGEST in c)
        activation = next(i for i,c in enumerate(calls) if "container_image_digest=" in c)
        self.assertLess(verify, activation)

    def test_build_failure_does_not_activate_service(self):
        result, calls = self.run_script("deploy-image.sh", BUILD_STATUS="7")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(any("container_image_digest=" in c for c in calls))
        self.assertFalse(any('"update-service"' in c for c in calls))

    def test_missing_digest_does_not_activate_service(self):
        result, calls = self.run_script("deploy-image.sh", RESULT_DIGEST="None")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("container_image_digest=" in c for c in calls))

    def test_rollback_verifies_prior_digest_without_build(self):
        result, calls = self.run_script("deploy-image.sh", GBRAIN_IMAGE_DIGEST=DIGEST)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("codebuild") or "-target=" in c for c in calls))
        self.assertTrue(any("imageDigest=" + DIGEST in c for c in calls))
        self.assertTrue(any("container_image_digest=" + DIGEST in c for c in calls))

    def test_rollback_unavailable_digest_does_not_activate(self):
        result, calls = self.run_script("deploy-image.sh", GBRAIN_IMAGE_DIGEST=DIGEST, REGISTRY_ERROR="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('"apply"' in c for c in calls))

    def test_existing_state_or_service_requires_profile_before_mutation(self):
        for existing in ({"STATE_EXISTS": "true"}, {"SERVICE_EXISTS": "true"}, {"STATE_ERROR": "true"}):
            with self.subTest(existing=existing):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script("deploy-image.sh", **existing)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any('"terraform"' in c or c.startswith("codebuild") for c in calls))

    def test_missing_profile_fails_before_mutation(self):
        result, calls = self.run_script("deploy-image.sh", GBRAIN_RUNTIME_TFVARS=str(self.root / "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('"terraform"' in c for c in calls))

    def test_reviewed_profile_is_reused_for_build_retry_and_rollback(self):
        profile = self.root / "runtime.tfvars.json"
        profile.write_text(json.dumps({"container_command": ["serve"], "container_entrypoint": ["/bin/sh"],
                                       "container_environment": [{"name": "EXTRA", "value": "retained"}],
                                       "service_subnet_ids": ["subnet-a", "subnet-b"]}))
        env = {"GBRAIN_RUNTIME_TFVARS": str(profile),
               "GBRAIN_RUNTIME_TFVARS_SHA256": hashlib.sha256(profile.read_bytes()).hexdigest()}
        for extra in ({}, {}, {"GBRAIN_IMAGE_DIGEST": DIGEST}):
            self.log.unlink(missing_ok=True)
            result, calls = self.run_script("deploy-image.sh", **env, **extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            applies = [json.loads(c) for c in calls if c.startswith('["terraform", "apply"')]
            self.assertEqual(len(applies), 1 if extra else 2)
            self.assertTrue(all("-var-file=" + str(profile) in args for args in applies))
        profile.write_text("{}")
        self.log.unlink(missing_ok=True)
        result, calls = self.run_script("deploy-image.sh", **env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('"terraform"' in c for c in calls))
        env["GBRAIN_RUNTIME_TFVARS_SHA256"] = hashlib.sha256(profile.read_bytes()).hexdigest()
        result, calls = self.run_script("deploy-image.sh", **env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('"terraform"' in c for c in calls))

if __name__ == "__main__":
    unittest.main()
