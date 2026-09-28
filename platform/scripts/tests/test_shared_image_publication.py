"""Execute the production publisher/deployer with isolated AWS, Docker and TF tools."""
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
        self.module = self.root / "repo/platform"
        (self.module / "scripts").mkdir(parents=True)
        (self.module / "terraform").mkdir()
        for script in ("publish-shared-image.sh", "resolve-ecr-image.py"):
            shutil.copy(MODULE / script, self.module / "scripts" / script)
        (self.root / "repo/modules/agent-factory").mkdir(parents=True)
        build = self.root / "repo/modules/gateway/scripts/stage-contracts.sh"
        build.parent.mkdir(parents=True)
        build.write_text('''#!/bin/bash
exit 0
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
 if args[0]=='run': sys.exit(int(os.environ.get('SELFCHECK_STATUS','0')))
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
        for name in ("GBRAIN_IMAGE_DIGEST", "BUILD_STATUS", "PUSH_STATUS", "MISSING_IMAGE", "REGISTRY_ERROR", "RESULT_DIGEST"):
            self.env.pop(name, None)

    def run_script(self, script, repo="adp-agent-runtime", **env):
        result = subprocess.run(["bash", str(self.module / "scripts" / script), repo],
                                env={**self.env, **env}, capture_output=True, text=True, timeout=10)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def test_first_publication(self):
        result, calls = self.run_script("publish-shared-image.sh", MISSING_IMAGE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum('"push"' in c for c in calls), 1)
        self.assertIn(DIGEST, result.stdout)

    def test_immutable_retry_reuses_existing_digest(self):
        result, calls = self.run_script("publish-shared-image.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('"docker"' in c for c in calls))
        self.assertIn("Reusing", result.stdout)

    def test_registry_error_does_not_rebuild(self):
        result, calls = self.run_script("publish-shared-image.sh", REGISTRY_ERROR="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('"docker"' in c for c in calls))

    def test_failed_build_never_pushes(self):
        result, calls = self.run_script("publish-shared-image.sh", MISSING_IMAGE="true", BUILD_STATUS="4")
        self.assertEqual(result.returncode, 4)
        self.assertFalse(any('"push"' in c for c in calls))

    def test_failed_push_is_failure(self):
        result, calls = self.run_script("publish-shared-image.sh", MISSING_IMAGE="true", PUSH_STATUS="5")
        self.assertEqual(result.returncode, 5)
        self.assertNotIn("Published", result.stdout)

    def test_publisher_rejects_missing_digest(self):
        result, _ = self.run_script("publish-shared-image.sh", RESULT_DIGEST="None")
        self.assertNotEqual(result.returncode, 0)

    def test_publisher_rejects_untrusted_source_tag(self):
        result, calls = self.run_script("publish-shared-image.sh", ADP_SOURCE_SHA="latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_all_four_build_contexts_publish(self):
        for repo in ("adp-gateway", "adp-chat-agent", "adp-agent-gateway", "adp-agent-runtime"):
            with self.subTest(repo=repo):
                Path(self.env["PUSHED"]).unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script("publish-shared-image.sh", repo=repo, MISSING_IMAGE="true")
                self.assertEqual(result.returncode, 0, result.stderr)
                pushes = [json.loads(c) for c in calls if c.startswith('["docker", "push"')]
                self.assertEqual(pushes, [["docker", "push", "example.invalid/" + repo + ":" + SHA]])
                self.assertIn("/" + repo + "@" + DIGEST, result.stdout)

    def test_failed_selfcheck_never_pushes(self):
        result, calls = self.run_script("publish-shared-image.sh", MISSING_IMAGE="true", SELFCHECK_STATUS="9")
        self.assertEqual(result.returncode, 9)
        self.assertFalse(any('"push"' in c for c in calls))

    def test_mutable_alias_is_rejected_before_build(self):
        result, calls = self.run_script("publish-shared-image.sh", PUBLISH_LATEST="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_tag_source_mismatch_is_rejected(self):
        result, calls = self.run_script("publish-shared-image.sh", IMAGE_TAG="c"*40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_resolver_pins_tag_and_checks_digest(self):
        script=self.module / "scripts/resolve-ecr-image.py"
        prefix="111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent"
        for selector in (":" + SHA, "@" + DIGEST):
            result=subprocess.run(["python3",str(script),prefix+selector],env=self.env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),prefix+"@"+DIGEST)

    def test_resolver_refuses_missing_mismatched_or_mutable_image(self):
        script=self.module / "scripts/resolve-ecr-image.py"
        prefix="111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent"
        for selector,extra in ((":latest",{}),(":"+SHA,{"RESULT_DIGEST":"None"}),("@"+DIGEST,{"RESULT_DIGEST":"sha256:"+"c"*64})):
            result=subprocess.run(["python3",str(script),prefix+selector],env={**self.env,**extra},text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)

if __name__ == "__main__":
    unittest.main()
