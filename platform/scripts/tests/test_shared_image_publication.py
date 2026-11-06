"""Execute the production publisher/deployer with isolated AWS, Docker and TF tools."""
import json
import os
from pathlib import Path
import re
import shutil
from string import Template
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
SHA = "b" * 40
SOURCE_ROOT = MODULE.parents[1]


def external_build_inputs(dockerfile, target=None, build_args=None):
    """Follow the real Dockerfile's FROM/COPY dependencies for one build target."""
    arguments = dict(build_args or {})
    stages = {}
    current = None
    lines = dockerfile.read_text().replace("\\\n", " ").splitlines()
    for line in lines:
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        instruction = words[0].upper()
        if instruction == "ARG" and "=" in words[1]:
            key, value = words[1].split("=", 1)
            arguments.setdefault(key, value)
        elif instruction == "FROM":
            source = next(word for word in words[1:] if not word.startswith("--"))
            name = words[-1] if words[-2].upper() == "AS" else str(len(stages))
            current = stages[name] = [source]
        elif current is not None and instruction in ("COPY", "ADD"):
            current.extend(re.findall(r"--from=(\S+)", line))
    pending = [target or next(reversed(stages))]
    visited = set()
    external = set()
    while pending:
        source = Template(pending.pop()).substitute(arguments)
        if source in visited or source == "scratch":
            continue
        visited.add(source)
        if source in stages:
            pending.extend(stages[source])
        else:
            external.add(source)
    return external


def docker_build_args(command):
    return dict(command[i + 1].split("=", 1) for i, value in enumerate(command) if value == "--build-arg")


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
        (build.parent.parent / "Dockerfile").write_text("FROM scratch\n")
        self.log = self.root / "calls"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = f'#!{sys.executable}\n' + '''import json,os,sys
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
 if args[0]=='build':
  if os.environ.get('FAIL_BUILD_TAG') and os.environ['FAIL_BUILD_TAG'] in args: sys.exit(7)
  sys.exit(int(os.environ.get('BUILD_STATUS','0')))
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
        for name in ("GBRAIN_IMAGE_DIGEST", "BUILD_STATUS", "FAIL_BUILD_TAG", "SELFCHECK_STATUS", "PUSH_STATUS", "MISSING_IMAGE", "REGISTRY_ERROR", "RESULT_DIGEST"):
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

    def assert_customer_prerequisites(self, repo, dockerfile, **env):
        result, calls = self.run_script("publish-shared-image.sh", repo=repo, MISSING_IMAGE="true", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [json.loads(c) for c in calls if c.startswith('["docker", "build"')]
        final = builds[-1]
        arguments = docker_build_args(final)
        local_images = {command[command.index("-t") + 1]: command for command in builds[:-1]}
        # Derive required inputs from the actual Dockerfile: a newly added
        # private artifact must also be handled by the customer publisher.
        private_inputs = {
            key for key, value in re.findall(r"^ARG (\w+)=(\S+)", dockerfile.read_text(), re.MULTILINE)
            if ".dkr.ecr." in value
        }
        self.assertTrue(private_inputs)
        self.assertFalse(private_inputs - arguments.keys(), f"Missing overrides: {private_inputs - arguments.keys()}")
        self.assertNotIn("--pull", final, "Locally built prerequisite tags must not be pulled remotely")
        if env.get("PUBLISH_LOCAL_BUILD") != "true":
            self.assertIn("--no-cache", final)
        for key in private_inputs:
            source = arguments[key]
            if repo == "adp-agent-gateway" and key == "AWSCLI_SECURITY_IMAGE":
                self.assertEqual(source, f"{self.env['REGISTRY']}/adp-agent-runtime:{SHA}")
                continue
            self.assertIn(source, local_images, f"{key} was not built before the deployable image")
            self.assertTrue(source.endswith(":" + SHA))
            command = local_images[source]
            self.assertIn("--pull", command)
            self.assertIn("--no-cache", command)
            context = SOURCE_ROOT / command[-1]
            recipe = SOURCE_ROOT / command[command.index("-f") + 1] if "-f" in command else context / "Dockerfile"
            target = command[command.index("--target") + 1] if "--target" in command else None
            # Only reachable stages matter. The curl and ARC Dockerfiles also
            # contain private overlays outside the selected artifact targets.
            inputs = external_build_inputs(recipe, target, docker_build_args(command))
            self.assertTrue(inputs)
            for image in inputs:
                self.assertRegex(image, r"^public\.ecr\.aws/.+@sha256:[0-9a-f]{64}$")
        resolved = external_build_inputs(dockerfile, build_args=arguments)
        self.assertFalse(any(".dkr.ecr." in source for source in resolved), resolved)
        return builds

    def test_customer_runtime_builds_every_private_prerequisite_from_public_sources(self):
        builds = self.assert_customer_prerequisites(
            "adp-agent-runtime", SOURCE_ROOT / "modules/agent-factory/agent-worker-image/Dockerfile")
        go = next(command for command in builds if f"adp-go-security:{SHA}" in command)
        self.assertEqual(go[go.index("--target") + 1], "go-security-artifact")
        self.assertEqual(go[-1], "platform/arc-runner")
        python = next(command for command in builds if f"adp-python314-expat:{SHA}" in command)
        self.assertEqual(docker_build_args(python)["ADP_SOURCE_COMMIT"], SHA)

    def test_local_customer_runtime_preserves_prerequisite_overrides(self):
        builds = self.assert_customer_prerequisites(
            "adp-agent-runtime", SOURCE_ROOT / "modules/agent-factory/agent-worker-image/Dockerfile",
            PUBLISH_LOCAL_BUILD="true")
        self.assertNotIn("--no-cache", builds[-1])

    def test_customer_agent_gateway_uses_public_curl_and_same_source_runtime(self):
        self.assert_customer_prerequisites(
            "adp-agent-gateway", SOURCE_ROOT / "modules/agent-factory/gateway/Dockerfile")

    def test_failed_runtime_prerequisite_never_builds_or_publishes_worker(self):
        for name in ("adp-curl-security", "adp-go-security", "adp-python314-expat"):
            with self.subTest(prerequisite=name):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script(
                    "publish-shared-image.sh", MISSING_IMAGE="true", FAIL_BUILD_TAG=f"{name}:{SHA}")
                self.assertEqual(result.returncode, 7)
                self.assertFalse(any("agent-worker-image/Dockerfile" in c for c in calls))
                self.assertFalse(any('"run"' in c or '"push"' in c for c in calls))

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
        # `sandbox-<sha>` is the restricted chat sandbox variant (#7899); still SHA-pinned.
        for selector in (":" + SHA, "@" + DIGEST, ":sandbox-" + SHA):
            result=subprocess.run([sys.executable,str(script),prefix+selector],env=self.env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),prefix+"@"+DIGEST)

    def test_resolver_refuses_missing_mismatched_or_mutable_image(self):
        script=self.module / "scripts/resolve-ecr-image.py"
        prefix="111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent"
        for selector,extra in ((":latest",{}),(":"+SHA,{"RESULT_DIGEST":"None"}),("@"+DIGEST,{"RESULT_DIGEST":"sha256:"+"c"*64})):
            result=subprocess.run([sys.executable,str(script),prefix+selector],env={**self.env,**extra},text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)

if __name__ == "__main__":
    unittest.main()
