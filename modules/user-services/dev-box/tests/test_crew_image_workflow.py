"""Crew image pipeline wiring (#8054): workflow/script parity, buildspec and IAM scope.

The ADP-managed workflow and the self-managed command must pin exactly the same
keys through the same scripts (AC-04); the bundle build publishes only under the
digest prefix the image build reads; every shell entrypoint parses.
"""

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MODULE_ROOT.parents[2]
WORKFLOWS = REPO_ROOT / ".github/workflows"
SCRIPTS = MODULE_ROOT / "scripts"
IMAGE = MODULE_ROOT / "image"
README = MODULE_ROOT / "README.md"
MANIFEST_DOC = REPO_ROOT / "docs/adp-platform-deployment/deployment-manifest.md"

SPEC = importlib.util.spec_from_file_location("dev_box_crew_release_pin_wiring", SCRIPTS / "crew_release_pin.py")
PIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIN)


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


ADP_CRED_FLAGS = {"--service", "--label", "--exec"}


def flags(command: str) -> set:
    return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", command)) - ADP_CRED_FLAGS


def test_workflow_dispatch_inputs_identity_and_guard_mirror_the_module_deploy():
    crew, deploy = workflow("dev-box-crew-image.yml"), workflow("dev-box-deploy.yml")
    inputs = crew.get("on", crew.get(True))["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"environment", "crew_model_id", "profile_revision"}
    assert inputs["environment"]["required"] and inputs["crew_model_id"]["required"] and not inputs["profile_revision"]["required"]
    assert crew["permissions"] == {"contents": "read"}
    job, deploy_job = crew["jobs"]["release"], deploy["jobs"]["deploy"]
    assert job["if"] == deploy_job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == deploy_job["environment"] == "dev-box-${{ inputs.environment }}"
    assert job["runs-on"] == deploy_job["runs-on"]
    steps = job["steps"]
    assert steps[0]["name"].startswith("Guard") and steps[1]["uses"].startswith("actions/checkout@") and steps[1]["with"]["persist-credentials"] is False
    deploy_text = (WORKFLOWS / "dev-box-deploy.yml").read_text()
    for variable in ("ADP_DEV_BOX_CONNECTION_LABEL", "ADP_DEV_BOX_ACCOUNT_ID", "ADP_DEV_BOX_REGION"):
        assert f"vars.{variable}" in steps[0]["env"].get({"ADP_DEV_BOX_CONNECTION_LABEL": "APPROVED_LABEL", "ADP_DEV_BOX_ACCOUNT_ID": "APPROVED_ACCOUNT", "ADP_DEV_BOX_REGION": "APPROVED_REGION"}[variable], "")
        assert f"vars.{variable}" in deploy_text, "the same connection identity as dev-box-deploy.yml"
    assert steps[2]["run"].count("adp-cred assume --service aws --label") == 1, "every cloud call goes through the connection"


@pytest.mark.parametrize("overrides,allowed", [
    ({}, True),
    ({"APPROVED_LABEL": ""}, False),
    ({"APPROVED_ACCOUNT": "12345"}, False),
    ({"APPROVED_REGION": ""}, False),
    ({"ENVIRONMENT": "Prod"}, False),
    ({"CREW_MODEL_ID": ""}, False),
    ({"CREW_MODEL_ID": "model id"}, False),
    ({"PROFILE_REVISION": "bad profile"}, False),
    ({"PROFILE_REVISION": ""}, True),
])
def test_workflow_guard_rejects_unapproved_dispatch_before_checkout(overrides, allowed):
    steps = workflow("dev-box-crew-image.yml")["jobs"]["release"]["steps"]
    environment = {"PATH": os.environ["PATH"], "APPROVED_LABEL": "approved", "APPROVED_ACCOUNT": "000000000000",
                   "APPROVED_REGION": "us-east-1", "ENVIRONMENT": "dev", "CREW_MODEL_ID": "us.anthropic.example-v1:0",
                   "PROFILE_REVISION": "crew-20261017-abcdefabcdef"}
    environment.update(overrides)
    result = subprocess.run(["bash", "-c", steps[0]["run"]], check=False, env=environment, capture_output=True)
    assert (result.returncode == 0) is allowed


def test_workflow_and_self_managed_command_run_the_same_script_with_the_same_pins():
    run = workflow("dev-box-crew-image.yml")["jobs"]["release"]["steps"][2]["run"]
    assert "modules/user-services/dev-box/scripts/release-crew-image.sh" in run
    assert not re.search(r"aws ssm put-parameter|build-dev-box-image\.sh|build-crew-bundle\.sh|packer|terraform", run), \
        "the workflow pins only through release-crew-image.sh"
    workflow_flags = flags(run)
    assert {"--env", "--region", "--crew-model-id", "--profile-revision", "--pin", "--account-id", "--journal", "--plan-output"} == workflow_flags
    readme = README.read_text()
    command = re.search(r"release-crew-image\.sh --env dev --crew-model-id <[^>]+> --apply", readme)
    assert command, "README documents the self-managed rollout command"
    assert flags(command.group(0)) - {"--apply"} <= workflow_flags - {"--pin"}
    script = (SCRIPTS / "release-crew-image.sh").read_text()
    for flag in workflow_flags | {"--apply", "--skip-image", "--skip-bundle", "--dry-run"}:
        assert f"{flag})" in script, f"release-crew-image.sh must accept {flag}"
    assert "crew_release_pin.py" in script and "aws ssm put-parameter" not in script, "the pin is written by one module only"
    assert PIN.SSM_PIN_KEYS == ("image-id", "image-revision", "profile-revision", "crew-model-id")


@pytest.mark.parametrize("key", PIN.SSM_PIN_KEYS)
def test_each_pinned_key_is_the_one_the_stage_reads_and_the_docs_name(key):
    stage = (SCRIPTS / "deploy-dev-box.sh").read_text()
    infra = (MODULE_ROOT / "infra/self_managed.tf").read_text()
    buildspec = (IMAGE / "buildspec.yml").read_text()
    if key == "crew-model-id":
        assert f'"{key}"' in infra, "Terraform publishes crew-model-id from crew_model_id (tfvars receive the same value)"
    else:
        assert f'"$SSM_PREFIX/{key}"' in stage, f"deploy-dev-box.sh reads {key}"
        assert f"put {key} " in buildspec, f"the image build publishes {key}"
    assert f"`{key}`" in README.read_text() or f"/{key}" in README.read_text()
    assert f"/adp/<env>/dev-box/{key}" in MANIFEST_DOC.read_text(), f"deployment-manifest.md must give a validation line for {key}"


def test_bundle_build_runs_the_same_script_and_publishes_only_under_the_digest_prefix():
    spec = yaml.safe_load((IMAGE / "bundle-buildspec.yml").read_text())
    assert spec["version"] == 0.2
    assert spec["phases"]["install"]["runtime-versions"] == {"python": 3.12}, "crew_bundle.py and kirocrew need 3.12"
    build = "\n".join(spec["phases"]["build"]["commands"])
    assert "scripts/build-crew-bundle.sh" in build and "--local" in build and '--source-revision "$ADP_SOURCE_SHA"' in build
    post = "\n".join(spec["phases"]["post_build"]["commands"])
    assert 'PREFIX="dev-box/image-inputs/${BUNDLE_SHA}"' in post
    assert "put crew-bundle-sha256" in post and "put crew-bundle-revision" in post
    assert len(re.findall(r"^\s*put ", post, re.M)) == 2, "the bundle build publishes exactly two parameters"
    assert "image-id" not in post, "the bundle build never touches the image pin"
    platform_tf = (MODULE_ROOT / "infra/platform/main.tf").read_text()
    for name in ("crew-bundle-sha256", "crew-bundle-revision"):
        assert f'"${{local.ssm_prefix}}/{name}"' in platform_tf
    assert 'Sid      = "PublishCrewBundleInputs"' in platform_tf and '"arn:aws:s3:::${var.state_bucket}/dev-box/image-inputs/*"' in platform_tf
    producer = (SCRIPTS / "build-crew-bundle.sh").read_text()
    assert 'CODEBUILD_BUILDSPEC_OVERRIDE="modules/user-services/dev-box/image/bundle-buildspec.yml"' in producer
    assert (REPO_ROOT / "modules/user-services/dev-box/image/bundle-buildspec.yml").is_file()
    assert "--buildspec-override" in (REPO_ROOT / "platform/scripts/codebuild-run.sh").read_text()
    assert 'dev-box/image-inputs/${BUNDLE_SHA}/crew.tar.gz' in producer and 'dev-box/image-inputs/${BUNDLE_SHA}/profile.json' in producer


def test_image_build_accepts_the_published_inputs_and_reverifies_the_digest():
    image_script = (SCRIPTS / "build-dev-box-image.sh").read_text()
    assert 'if [[ "$CREW_BUNDLE" == s3://* ]] || [[ "$CREW_MANIFEST" == s3://* ]]; then' in image_script
    assert 'prefix = f"s3://{sys.argv[5]}/dev-box/image-inputs/{digest}/"' in image_script, "remote inputs must sit under the digest prefix"
    assert "aws s3api head-object" in image_script
    assert 'INPUT_PREFIX="dev-box/image-inputs/$(sha256sum "$CREW_BUNDLE" | cut -d\' \' -f1)"' in image_script, "local inputs still upload by digest"
    buildspec = (IMAGE / "buildspec.yml").read_text()
    assert "BUNDLE_SHA=$(sha256sum /tmp/crew-inputs/crew.tar.gz | cut -d' ' -f1)" in buildspec
    assert 'assert m.get("bundle_sha256")==sys.argv[3]' in buildspec
    assert '"s3://adp-terraform-state-${ACCOUNT_ID}/dev-box/image-inputs/${BUNDLE_SHA}/crew.tar.gz"' in buildspec, "the URI handed to the instance is the verified digest prefix"
    assert '-var "crew_bundle_s3_uri=$CREW_BUNDLE_S3_URI" -var "crew_bundle_sha256=$BUNDLE_SHA"' in buildspec
    assert "crew_bundle_path" not in buildspec
    assert "MiB, sha256 $BUNDLE_SHA" in buildspec, "the bundle size is printed before the Packer build"
    assert "crew bundle: ${BUNDLE_URI} ($(( ${BUNDLE_BYTES:-0} / 1048576 )) MiB" in image_script
    platform_tf = (MODULE_ROOT / "infra/platform/main.tf").read_text()
    assert 'Sid      = "ReadCrewBundleInputs"' in platform_tf and 'name  = "com.amazonaws.${var.region}.s3"' in platform_tf
    crew_template = (IMAGE / "crew.pkr.hcl").read_text()
    assert '"adp:profile-revision" = var.profile_revision' in crew_template, "the tag release-crew-image.sh reads back"
    release = (SCRIPTS / "release-crew-image.sh").read_text()
    assert '"adp:profile-revision": sys.argv[2]' in release and "aws ec2 describe-images" in release


@pytest.mark.parametrize("script", ["build-crew-bundle.sh", "release-crew-image.sh", "build-dev-box-image.sh", "deploy-dev-box.sh"])
def test_shell_entrypoints_parse(script):
    subprocess.run(["bash", "-n", str(SCRIPTS / script)], check=True)
    assert os.access(SCRIPTS / script, os.X_OK), f"{script} must be executable"


def test_module_ci_covers_the_pipeline():
    ci = (WORKFLOWS / "dev-box-ci.yml").read_text()
    for item in ("test_crew_bundle.py", "test_release_crew_image.py", "test_crew_image_workflow.py",
                 "scripts/build-crew-bundle.sh", "scripts/release-crew-image.sh", "image/bundle-buildspec.yml"):
        assert item in ci, f"dev-box-ci.yml must exercise {item}"
    assert ".github/workflows/dev-box-crew-image.yml" in ci
