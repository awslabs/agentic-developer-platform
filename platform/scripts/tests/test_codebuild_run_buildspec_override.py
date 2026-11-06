"""codebuild-run.sh may run an alternate buildspec from the same source zip (#8054).

The Dev Box image project runs image/buildspec.yml by default; the Crew bundle
build reuses the project with image/bundle-buildspec.yml through
CODEBUILD_BUILDSPEC_OVERRIDE. The first live run failed because the override was
inserted inside the --environment-variables-override list, so the CLI saw the
caller's "name=...,value=...,type=PLAINTEXT" entries as unknown options; the
argv test below drives the script against a shimmed `aws` (and `zip`) and
asserts the exact vector handed to `aws codebuild start-build`. No build starts.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "platform/scripts/codebuild-run.sh"

AWS_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["AWS_SHIM_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["codebuild", "start-build"]:
    print("example-project:build-1"); sys.exit(0)
if args[:2] == ["codebuild", "batch-get-builds"]:
    print(json.dumps({"s": "SUCCEEDED", "p": "COMPLETED"})); sys.exit(0)
sys.exit(0)
'''
ZIP_SHIM = "#!/bin/sh\n# test shim: write an empty archive instead of zipping the repository\nfor a in \"$@\"; do case \"$a\" in *.zip) : > \"$a\";; esac; done\n"


def run_codebuild(overrides, buildspec_override=None, compute_override=None):
    with tempfile.TemporaryDirectory() as scratch:
        shim = Path(scratch) / "bin"
        shim.mkdir()
        (shim / "aws").write_text(AWS_SHIM)
        (shim / "aws").chmod(0o755)
        (shim / "zip").write_text(ZIP_SHIM)
        (shim / "zip").chmod(0o755)
        log = Path(scratch) / "calls.jsonl"
        env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "AWS_SHIM_LOG": str(log), "STATE_BUCKET": "example-bucket",
               "AWS_REGION": "us-east-1", "SOURCE_SHA": "a" * 40, "POLL_INTERVAL": "0"}
        env.pop("CODEBUILD_BUILDSPEC_OVERRIDE", None)
        env.pop("CODEBUILD_COMPUTE_TYPE_OVERRIDE", None)
        if buildspec_override:
            env["CODEBUILD_BUILDSPEC_OVERRIDE"] = buildspec_override
        if compute_override:
            env["CODEBUILD_COMPUTE_TYPE_OVERRIDE"] = compute_override
        result = subprocess.run(["bash", str(SCRIPT), "example-project", *overrides], env=env, capture_output=True, text=True, check=False)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls


def start_build_argv(calls):
    return next(call for call in calls if call[:2] == ["codebuild", "start-build"])


class CodebuildRunBuildspecOverrideTests(unittest.TestCase):
    def test_override_is_optional_and_passed_once(self):
        source = SCRIPT.read_text()
        self.assertIn('if [ -n "${CODEBUILD_BUILDSPEC_OVERRIDE:-}" ]; then', source)
        self.assertIn('CMD+=(--buildspec-override "$CODEBUILD_BUILDSPEC_OVERRIDE")', source)
        self.assertEqual(source.count("--buildspec-override"), 2, "documented once, applied once")
        self.assertIn("CODEBUILD_BUILDSPEC_OVERRIDE", source.split("set -euo pipefail")[0], "the header documents the variable")
        start = source.index("CMD=(aws codebuild start-build")
        self.assertLess(start, source.index('CMD+=(--buildspec-override'), "the override extends the start-build command")
        self.assertLess(source.index('CMD+=(--buildspec-override'), source.index("CMD+=(--query 'build.id'"))

    def test_only_the_dev_box_bundle_build_uses_it(self):
        result = subprocess.run(["grep", "-rl", "--include=*.sh", "--include=*.yml", "--include=*.py", "--exclude-dir=node_modules",
                                 "--exclude-dir=.terraform", "--exclude-dir=.git", "--exclude-dir=tests", "CODEBUILD_BUILDSPEC_OVERRIDE=",
                                 "modules", "platform", ".github"], capture_output=True, text=True, check=False, cwd=ROOT)
        users = sorted(result.stdout.split())
        self.assertEqual(users, ["modules/user-services/dev-box/scripts/build-crew-bundle.sh"])
        producer = (ROOT / users[0]).read_text()
        override = re.search(r'CODEBUILD_BUILDSPEC_OVERRIDE="([^"]+)"', producer).group(1)
        self.assertTrue((ROOT / override).is_file(), override)

    def test_script_parses(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_environment_overrides_stay_one_contiguous_list_with_a_buildspec_override(self):
        overrides = ["name=BUNDLE_REVISION,value=crew-20261017-abcdefabcdef,type=PLAINTEXT",
                     "name=SOURCE_DATE_EPOCH,value=1792258146,type=PLAINTEXT"]
        result, calls = run_codebuild(overrides, "modules/user-services/dev-box/image/bundle-buildspec.yml", "BUILD_GENERAL1_MEDIUM")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = start_build_argv(calls)
        start = argv.index("--environment-variables-override")
        values = argv[start + 1:start + 4]
        self.assertEqual(values, ["name=ADP_SOURCE_SHA,value=" + "a" * 40 + ",type=PLAINTEXT", *overrides],
                         "each override is its own argv element, directly after the option")
        self.assertTrue(all(not value.startswith("--") for value in values))
        self.assertEqual(argv[start + 4], "--buildspec-override", "the buildspec override comes after the list")
        self.assertEqual(argv[start + 5], "modules/user-services/dev-box/image/bundle-buildspec.yml")
        self.assertEqual(argv[start + 6:start + 8], ["--compute-type-override", "BUILD_GENERAL1_MEDIUM"], "per-build compute, not a project change")
        self.assertEqual(argv[start + 8:], ["--query", "build.id", "--output", "text"])
        self.assertEqual(argv.count("--buildspec-override"), 1)
        self.assertNotIn(", ", " ".join(argv), "overrides are never comma-joined into one string")

    def test_compute_override_must_be_a_codebuild_compute_type(self):
        result, calls = run_codebuild(["name=X,value=1,type=PLAINTEXT"], None, "BUILD_GENERAL1_MEDIUM; rm -rf /")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a CodeBuild compute type", result.stderr)
        self.assertFalse(any(call[:2] == ["codebuild", "start-build"] for call in calls))

    def test_without_an_override_the_vector_is_unchanged_for_the_image_build(self):
        overrides = ["name=IMAGE_REVISION,value=,type=PLAINTEXT", "name=SOURCE_AMI,value=,type=PLAINTEXT"]
        result, calls = run_codebuild(overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = start_build_argv(calls)
        self.assertNotIn("--buildspec-override", argv)
        self.assertNotIn("--compute-type-override", argv, "the Packer image build keeps the project's compute")
        start = argv.index("--environment-variables-override")
        self.assertEqual(argv[start + 1:], ["name=ADP_SOURCE_SHA,value=" + "a" * 40 + ",type=PLAINTEXT", *overrides,
                                            "--query", "build.id", "--output", "text"])


if __name__ == "__main__":
    unittest.main()
