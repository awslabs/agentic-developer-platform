"""Run local publication and standalone deployment entry points without cloud writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"
DIGEST = "sha256:" + "a" * 64

TOOL = r"""#!/usr/bin/python3
import json, os, sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
row={"tool":name,"args":args}
if name=="docker" and args[0]=="build":
 context=Path(args[-1]).resolve()
 row["context"]=str(context)
 row["marker"]=(context/"source-marker").read_text()
if name=="kubectl" and args[0]=="apply":
 row["manifest"]=Path(args[-1]).read_text()
with open(os.environ["CALLS"],"a") as f:f.write(json.dumps(row)+"\n")
if name=="aws":
 if args[:2]==["sts","get-caller-identity"]: print("111122223333")
 elif args[:2]==["ecr","get-login-password"]: print("test-login")
 elif args[:2]==["ecr","describe-images"]:
  mode=os.environ.get("REGISTRY_MODE","missing")
  if mode=="denied": print("AccessDeniedException",file=sys.stderr);sys.exit(31)
  if mode=="missing" and not Path(os.environ["PUSHED"]).exists():
   print("ImageNotFoundException",file=sys.stderr);sys.exit(32)
  print(os.environ.get("RESULT_DIGEST","sha256:"+"a"*64))
 else: print("Unexpected AWS mutation",file=sys.stderr);sys.exit(99)
elif name=="docker":
 if args[0]=="login":sys.stdin.read()
 if args[0]=="build":sys.exit(int(os.environ.get("BUILD_EXIT","0")))
 if args[0]=="run":sys.exit(int(os.environ.get("SELFCHECK_EXIT","0")))
 if args[0]=="push":
  if os.environ.get("PUSH_EXIT"):sys.exit(int(os.environ["PUSH_EXIT"]))
  Path(os.environ["PUSHED"]).touch()
elif name=="terraform":
 if args[0]=="output":print("fixture-output")
 else: print("Unexpected Terraform mutation",file=sys.stderr);sys.exit(98)
"""


class LocalPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        for relative in (
            "platform/scripts/publish-local-image.sh",
            "platform/scripts/publish-shared-image.sh",
            "platform/scripts/resolve-ecr-image.py",
            "modules/agent-factory/scripts/deploy-gateway.sh",
            "modules/agent-factory/gateway/config.env",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        for context in (
            self.root,
            self.root / "modules/gateway",
            self.root / "modules/agent-factory",
            self.root / "platform/security/curl-8.22.0",
            self.root / "platform/arc-runner",
        ):
            context.mkdir(parents=True, exist_ok=True)
            (context / "source-marker").write_text("committed input")
        stage = self.root / "modules/gateway/scripts/stage-contracts.sh"
        stage.parent.mkdir()
        stage.write_text("#!/bin/bash\nexit 0\n")
        (self.root / "modules/agent-factory/infra").mkdir()
        manifest = self.root / "modules/agent-factory/gateway/k8s/keda-scaledjob.yaml"
        manifest.parent.mkdir()
        manifest.write_text("image: REPLACE_WITH_AGENT_IMAGE\n")
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "."],
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(command, cwd=self.root, check=True, capture_output=True)
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("aws", "docker", "terraform", "kubectl"):
            script = self.bin / name
            script.write_text(TOOL)
            script.chmod(0o755)
        self.calls = self.base / "calls"
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "ADP_"))
        }
        for key in (
            "AGENT_IMAGE",
            "AGENT_IMAGE_TAG",
            "IMAGE_TAG",
            "PUBLISH_LATEST",
            "SOURCE_SHA",
            "REGISTRY_MODE",
            "RESULT_DIGEST",
            "BUILD_EXIT",
            "PUSH_EXIT",
            "SELFCHECK_EXIT",
        ):
            self.env.pop(key, None)
        self.env.update(
            PATH=f"{self.bin}:/usr/bin:/bin",
            CALLS=str(self.calls),
            PUSHED=str(self.base / "pushed"),
            REGISTRY=REGISTRY,
            AWS_REGION="us-east-1",
            TMPDIR=str(self.base),
        )

    def invoke(self, script, args=(), **extra):
        result = subprocess.run(
            ["bash", str(self.root / script), *args],
            cwd=self.base,
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            timeout=20,
        )
        calls = (
            [json.loads(line) for line in self.calls.read_text().splitlines()]
            if self.calls.exists()
            else []
        )
        return result, calls

    def publish(self, repo="adp-agent-gateway", **extra):
        return self.invoke("platform/scripts/publish-local-image.sh", [repo], **extra)

    def test_local_builds_archive_the_selected_commit_for_all_four_families(self):
        for repo in (
            "adp-gateway",
            "adp-agent-gateway",
            "adp-chat-agent",
            "adp-agent-runtime",
        ):
            with self.subTest(repo=repo):
                self.calls.unlink(missing_ok=True)
                (self.base / "pushed").unlink(missing_ok=True)
                # A locally modified input cannot be published under the committed SHA.
                for path in self.root.rglob("source-marker"):
                    path.write_text("uncommitted input")
                result, calls = self.publish(repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                builds = [
                    c
                    for c in calls
                    if c["tool"] == "docker" and c["args"][0] == "build"
                ]
                self.assertEqual(len(builds), {
                    "adp-gateway": 1,
                    "adp-agent-gateway": 2,
                    "adp-chat-agent": 1,
                    "adp-agent-runtime": 3,
                }[repo])
                self.assertTrue(all(build["marker"] == "committed input" for build in builds))
                self.assertNotIn("--no-cache", builds[-1]["args"])
                self.assertNotIn("--pull", builds[-1]["args"])
                self.assertTrue(all(str(self.root) not in build["context"] for build in builds))
                pushes = [
                    c["args"]
                    for c in calls
                    if c["tool"] == "docker" and c["args"][0] == "push"
                ]
                self.assertEqual(pushes, [["push", f"{REGISTRY}/{repo}:{self.sha}"]])
                self.assertIn(f"{REGISTRY}/{repo}@{DIGEST}", result.stdout)
                self.assertFalse(list(self.base.glob("adp-local-image.*")))

    def test_retry_reuses_registry_digest_without_docker_or_push(self):
        result, calls = self.publish(REGISTRY_MODE="existing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c["tool"] == "docker" for c in calls))

    def test_old_source_uses_the_current_reviewed_publisher(self):
        helper = self.root / "platform/scripts/publish-shared-image.sh"
        body = helper.read_bytes()
        subprocess.run(
            ["git", "rm", "platform/scripts/publish-shared-image.sh"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Snapshot without publication helper",
            ],
            cwd=self.root,
            check=True,
        )
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()
        helper.write_bytes(body)
        result, calls = self.publish(SOURCE_SHA=revision)
        self.assertEqual(result.returncode, 0, result.stderr)
        pushes = [
            c["args"] for c in calls if c["tool"] == "docker" and c["args"][0] == "push"
        ]
        self.assertEqual(pushes, [["push", f"{REGISTRY}/adp-agent-gateway:{revision}"]])

    def test_real_deploy_all_gateway_phase_resolves_digest_after_all_selfchecks(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index(
            "  # Migrations run after rollout", source.index('step "Step 4/12:')
        )
        block = source[start : source.index("  # --- K8s deploy", start)]
        result = subprocess.run(
            [
                "bash",
                "-c",
                'set -euo pipefail\nfail() { echo "$*" >&2; exit 1; }\n'
                + block
                + '\nprintf "%s\\n" "$GATEWAY_IMAGE"',
            ],
            env={
                **self.env,
                "ROOT_DIR": str(self.root),
                "SOURCE_SHA": self.sha,
                "IMAGE_TAG": self.sha,
                "LOCAL_MODE": "true",
                "ENVIRONMENT": "dev",
                "GATEWAY_IMAGE": f"{REGISTRY}/adp-gateway:{self.sha}",
            },
            cwd=self.base,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[-1], f"{REGISTRY}/adp-gateway@{DIGEST}"
        )
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        checks = [
            c["args"][-1]
            for c in calls
            if c["tool"] == "docker" and c["args"][0] == "run"
        ]
        self.assertEqual(
            checks,
            [
                "pricing_policy.selfcheck",
                "src.orchestration.review_contract_selfcheck",
                "src.orchestration.evaluation_contract_selfcheck",
            ],
        )

    def test_registry_denial_never_builds_or_creates_repository(self):
        result, calls = self.publish(REGISTRY_MODE="denied")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c["tool"] == "docker" for c in calls))
        self.assertFalse(any("create-repository" in c["args"] for c in calls))

    def test_bad_tags_and_mutable_alias_fail_before_cloud_calls(self):
        for extra in (
            {"SOURCE_SHA": "latest"},
            {"IMAGE_TAG": "bad"},
            {"PUBLISH_LATEST": "true"},
        ):
            with self.subTest(extra=extra):
                result, calls = self.publish(**extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_failed_build_or_gateway_selfcheck_never_pushes(self):
        for extra in ({"BUILD_EXIT": "23"}, {"SELFCHECK_EXIT": "24"}):
            with self.subTest(extra=extra):
                self.calls.unlink(missing_ok=True)
                result, calls = self.publish("adp-gateway", **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(
                    any(c["tool"] == "docker" and c["args"][0] == "push" for c in calls)
                )

    def test_standalone_gateway_promotes_only_verified_digest(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh", ["--skip-terraform"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        applies = [
            c for c in calls if c["tool"] == "kubectl" and c["args"][0] == "apply"
        ]
        self.assertEqual(len(applies), 1)
        self.assertEqual(
            applies[0]["manifest"], f"image: {REGISTRY}/adp-agent-gateway@{DIGEST}\n"
        )

    def test_standalone_build_failure_stops_before_workload_promotion(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh",
            ["--skip-terraform"],
            BUILD_EXIT="23",
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(any(c["tool"] == "kubectl" for c in calls))

    def test_standalone_invalid_tag_fails_before_terraform_or_kubernetes(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh", AGENT_IMAGE_TAG="latest"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            any(c["tool"] in ("terraform", "kubectl", "docker") for c in calls)
        )

    def test_skip_build_verifies_rollback_digest_without_publication(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh",
            ["--skip-terraform", "--skip-image-build"],
            AGENT_IMAGE=f"{REGISTRY}/adp-agent-gateway@{DIGEST}",
            REGISTRY_MODE="existing",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c["tool"] == "docker" for c in calls))

    def test_skip_build_requires_an_explicit_immutable_selector(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh", ["--skip-image-build"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            any(c["tool"] in ("terraform", "kubectl", "docker") for c in calls)
        )

    def test_dry_run_does_not_publish_or_contact_cloud_services(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh", ["--dry-run"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_skipping_both_image_and_workload_does_not_require_an_image(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh",
            ["--skip-terraform", "--skip-image-build", "--skip-k8s"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c["tool"] in ("aws", "kubectl", "docker") for c in calls))

    def test_bad_rollback_digest_fails_before_any_mutation(self):
        result, calls = self.invoke(
            "modules/agent-factory/scripts/deploy-gateway.sh",
            ["--skip-image-build"],
            AGENT_IMAGE=f"{REGISTRY}/adp-agent-gateway@{DIGEST}",
            REGISTRY_MODE="existing",
            RESULT_DIGEST="sha256:" + "c" * 64,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            any(c["tool"] in ("terraform", "kubectl", "docker") for c in calls)
        )


if __name__ == "__main__":
    unittest.main()
