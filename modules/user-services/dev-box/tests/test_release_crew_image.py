"""Crew image release pin (#8054): plan without writes, pin on request, refuse a foreign AMI.

AWS is replaced by an executable shim on PATH that answers from a JSON state file
and records every call; nothing here reaches an account, CodeBuild or a cluster.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MODULE_ROOT.parents[2]
SCRIPTS = MODULE_ROOT / "scripts"
RELEASE_SCRIPT = SCRIPTS / "release-crew-image.sh"
DEV_TFVARS = REPO_ROOT / "environments/dev/modules/dev-box.tfvars"

SPEC = importlib.util.spec_from_file_location("dev_box_crew_release_pin", SCRIPTS / "crew_release_pin.py")
PIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIN)

ACCOUNT = "879318057152"
SOURCE = subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True).strip()
AMI = "ami-0123456789abcdef0"
PROFILE = "crew-20261017-abcdefabcdef"
IMAGE_REVISION = f"base-20261017-abcdefabcdef-crew-{PROFILE}"
MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

AWS_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["AWS_SHIM_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["AWS_SHIM_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default
service, action = args[0], args[1]
if service == "sts":
    print(state["account"]); sys.exit(0)
if service == "ssm" and action == "get-parameter":
    name = option("--name")
    if name in state["ssm"]:
        print(state["ssm"][name]); sys.exit(0)
    sys.stderr.write("ParameterNotFound\n"); sys.exit(255)
if service == "ssm" and action == "put-parameter":
    state["ssm"][option("--name")] = option("--value")
    json.dump(state, open(state_path, "w")); sys.exit(0)
if service == "ec2" and action == "describe-images":
    print(json.dumps(state["tags"].get(option("--image-ids")))); sys.exit(0)
if service in ("s3", "s3api"):
    sys.exit(0)
sys.stderr.write("unexpected aws call: " + " ".join(args) + "\n"); sys.exit(1)
'''


@pytest.fixture
def aws(tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "aws").write_text(AWS_SHIM)
    (shim / "aws").chmod(0o755)
    state = tmp_path / "aws-state.json"
    log = tmp_path / "aws-calls.jsonl"
    prefix = "/adp/dev/dev-box"
    state.write_text(json.dumps({
        "account": ACCOUNT,
        "ssm": {f"{prefix}/image-id": AMI, f"{prefix}/image-revision": IMAGE_REVISION, f"{prefix}/profile-revision": PROFILE,
                f"{prefix}/image-source-revision": SOURCE, f"{prefix}/base-image-id": "ami-0fedcba9876543210",
                f"{prefix}/image-builder-project": "adp-dev-dev-box-image"},
        "tags": {AMI: [{"Key": "adp:component", "Value": "dev-box"}, {"Key": "adp:profile-revision", "Value": PROFILE},
                       {"Key": "adp:image-revision", "Value": IMAGE_REVISION}, {"Key": "adp:source-revision", "Value": SOURCE}]},
    }))
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "AWS_SHIM_STATE": str(state), "AWS_SHIM_LOG": str(log)}
    env.pop("AWS_PROFILE", None)

    class Shim:
        def __init__(self):
            self.env, self.state_path, self.log_path = env, state, log

        @property
        def state(self):
            return json.loads(state.read_text())

        def set_state(self, **changes):
            data = self.state
            for key, value in changes.items():
                data[key] = value
            state.write_text(json.dumps(data))

        @property
        def calls(self):
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

        def puts(self):
            return {call[call.index("--name") + 1]: call[call.index("--value") + 1] for call in self.calls if call[:2] == ["ssm", "put-parameter"]}

    return Shim()


@pytest.fixture
def tfvars(tmp_path):
    copy = tmp_path / "dev-box.tfvars"
    shutil.copy(DEV_TFVARS, copy)
    return copy


def release(tmp_path, aws, *arguments):
    return subprocess.run(["bash", str(RELEASE_SCRIPT), "--env", "dev", "--region", "us-east-1", *arguments],
                          capture_output=True, text=True, check=False, env=aws.env, cwd=tmp_path)


# --- crew_release_pin.py ------------------------------------------------------
def test_plan_lists_the_pins_and_the_tfvars_diff_without_writing(tfvars):
    before = tfvars.read_text()
    plan = PIN.plan(environment="dev", image_id=AMI, image_revision=IMAGE_REVISION, profile_revision=PROFILE,
                    crew_model_id=MODEL, source_revision=SOURCE, bundle_sha256="b" * 64, tfvars_path=tfvars)
    assert plan["ssm"] == {"/adp/dev/dev-box/image-id": AMI, "/adp/dev/dev-box/image-revision": IMAGE_REVISION,
                           "/adp/dev/dev-box/profile-revision": PROFILE, "/adp/dev/dev-box/crew-model-id": MODEL}
    assert list(plan["ssm"]) == [f"/adp/dev/dev-box/{key}" for key in PIN.SSM_PIN_KEYS]
    assert plan["github_variables"] == {"ADP_DEV_BOX_IMAGE_ID": AMI, "ADP_DEV_BOX_IMAGE_REVISION": IMAGE_REVISION,
                                        "ADP_DEV_BOX_CREW_PROFILE_REVISION": PROFILE, "ADP_DEV_BOX_CREW_MODEL_ID": MODEL}
    assert plan["tfvars"]["present"] and plan["tfvars"]["changed"]
    assert f'+crew_model_id = "{MODEL}"' in plan["tfvars"]["diff"]
    assert '-# crew_model_id    = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"' in plan["tfvars"]["diff"]
    assert tfvars.read_text() == before
    text = PIN.render(plan)
    assert f"/adp/dev/dev-box/image-id = {AMI}" in text and "tfvars diff" in text and "ADP_DEV_BOX_CREW_MODEL_ID=" in text


def test_rewrite_tfvars_replaces_uncomments_or_appends_and_is_idempotent():
    active = 'account_id = "1"\ncrew_model_id = "old-model"   # approved\nregion = "us-east-1"\n'
    assert PIN.rewrite_tfvars(active, MODEL) == f'account_id = "1"\ncrew_model_id = "{MODEL}"   # approved\nregion = "us-east-1"\n'
    commented = 'dashboard_enabled = true\n# crew_model_id    = "x"   # set with the Crew image\n'
    uncommented = PIN.rewrite_tfvars(commented, MODEL)
    assert uncommented == f'dashboard_enabled = true\ncrew_model_id = "{MODEL}"   # pinned by release-crew-image.sh\n'
    absent = 'dashboard_enabled = true'
    appended = PIN.rewrite_tfvars(absent, MODEL)
    assert appended.endswith(f'\n# Pinned by modules/user-services/dev-box/scripts/release-crew-image.sh\ncrew_model_id = "{MODEL}"\n')
    for text in (uncommented, appended):
        assert PIN.rewrite_tfvars(text, MODEL) == text
    plan = PIN.plan(environment="dev", image_id=AMI, image_revision=IMAGE_REVISION, profile_revision=PROFILE, crew_model_id=MODEL)
    assert plan["tfvars"] == {"path": "", "present": False, "changed": False, "diff": "", "after": ""}


@pytest.mark.parametrize("overrides,message", [
    ({"image_id": "ami-zz"}, "invalid image id"),
    ({"image_revision": "base-20261017-abcdefabcdef"}, "was not built with Crew profile"),
    ({"profile_revision": "bad profile"}, "invalid Crew profile revision"),
    ({"crew_model_id": ""}, "invalid Crew model id"),
    ({"crew_model_id": "model with spaces"}, "invalid Crew model id"),
    ({"source_revision": "main"}, "full commit"),
    ({"environment": "Prod"}, "invalid environment"),
])
def test_plan_refuses_an_unpinned_or_foreign_release(overrides, message):
    arguments = dict(environment="dev", image_id=AMI, image_revision=IMAGE_REVISION, profile_revision=PROFILE,
                     crew_model_id=MODEL, source_revision=SOURCE)
    arguments.update(overrides)
    with pytest.raises(ValueError, match=message):
        PIN.plan(**arguments)


def test_apply_writes_each_pin_the_tfvars_and_a_journal_line(tfvars, tmp_path):
    plan = PIN.plan(environment="dev", image_id=AMI, image_revision=IMAGE_REVISION, profile_revision=PROFILE,
                    crew_model_id=MODEL, source_revision=SOURCE, tfvars_path=tfvars)
    calls = []

    def run(command, **kwargs):
        calls.append(command)

    journal = tmp_path / "release/journal.jsonl"
    entry = PIN.apply(plan, region="us-east-1", journal_path=journal, run=run)
    assert [command[:3] for command in calls] == [["aws", "ssm", "put-parameter"]] * 4
    assert {command[command.index("--name") + 1]: command[command.index("--value") + 1] for command in calls} == plan["ssm"]
    assert all("--overwrite" in command and "--region" in command for command in calls)
    assert f'crew_model_id = "{MODEL}"' in tfvars.read_text() and not tfvars.with_suffix(".tfvars.tmp").exists()
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert lines == [entry] and entry["action"] == "crew-image-release-pin" and entry["tfvars_changed"] is True
    assert entry["ssm"] == plan["ssm"] and entry["timestamp"].endswith("Z")


def test_cli_plan_only_then_apply_through_the_aws_shim(aws, tfvars, tmp_path, capsys):
    arguments = ["--env", "dev", "--image-id", AMI, "--image-revision", IMAGE_REVISION, "--profile-revision", PROFILE,
                 "--crew-model-id", MODEL, "--tfvars", str(tfvars), "--journal", str(tmp_path / "j.jsonl"),
                 "--plan-output", str(tmp_path / "plan.json")]
    assert PIN.main(arguments) == 0
    out = capsys.readouterr().out
    assert "plan only: nothing written" in out and aws.puts() == {} and "crew_model_id    =" in tfvars.read_text()
    assert json.loads((tmp_path / "plan.json").read_text())["ssm"]["/adp/dev/dev-box/crew-model-id"] == MODEL
    os.environ.update({k: aws.env[k] for k in ("PATH", "AWS_SHIM_STATE", "AWS_SHIM_LOG")})
    try:
        assert PIN.main(arguments + ["--apply"]) == 0
    finally:
        os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]
    assert aws.puts() == {f"/adp/dev/dev-box/{key}": value for key, value in
                          zip(PIN.SSM_PIN_KEYS, (AMI, IMAGE_REVISION, PROFILE, MODEL))}
    assert "pinned: 4 SSM parameters written, tfvars updated" in capsys.readouterr().out
    assert PIN.main(arguments + ["--image-id", "nope"]) == 2


# --- release-crew-image.sh ----------------------------------------------------
def test_release_skip_image_plans_without_writes(aws, tfvars, tmp_path):
    before = tfvars.read_text()
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--profile-revision", PROFILE, "--skip-image", "--tfvars", str(tfvars),
                     "--journal", str(tmp_path / "journal.jsonl"))
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"Image {AMI}: revision {IMAGE_REVISION}, profile {PROFILE}" in result.stdout and "(tags verified)" in result.stdout
    assert f"/adp/dev/dev-box/image-id = {AMI}" in result.stdout
    assert f"/adp/dev/dev-box/profile-revision = {PROFILE}" in result.stdout
    assert f"/adp/dev/dev-box/crew-model-id = {MODEL}" in result.stdout
    assert f'+crew_model_id = "{MODEL}"' in result.stdout and "plan only" in result.stdout
    assert tfvars.read_text() == before and aws.puts() == {} and not (tmp_path / "journal.jsonl").exists()
    assert not any(call[0] == "codebuild" for call in aws.calls)


def test_release_pin_writes_the_four_keys_and_journals(aws, tfvars, tmp_path):
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--profile-revision", PROFILE, "--skip-image", "--tfvars", str(tfvars),
                     "--journal", str(tmp_path / "journal.jsonl"), "--plan-output", str(tmp_path / "pins.json"), "--pin",
                     "--account-id", ACCOUNT)
    assert result.returncode == 0, result.stderr + result.stdout
    assert aws.puts() == {f"/adp/dev/dev-box/{key}": value for key, value in zip(PIN.SSM_PIN_KEYS, (AMI, IMAGE_REVISION, PROFILE, MODEL))}
    assert f'crew_model_id = "{MODEL}"' in tfvars.read_text()
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert len(journal) == 1 and journal[0]["source_revision"] == SOURCE
    assert json.loads((tmp_path / "pins.json").read_text())["github_variables"]["ADP_DEV_BOX_IMAGE_ID"] == AMI
    assert "deploy-dev-box.sh --env dev --skip-image --skip-verify" in result.stdout


def test_release_apply_dry_run_names_the_stage_without_running_it(aws, tfvars, tmp_path):
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--profile-revision", PROFILE, "--skip-image", "--tfvars", str(tfvars),
                     "--journal", str(tmp_path / "journal.jsonl"), "--apply", "--dry-run")
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"dry-run: bash {SCRIPTS}/deploy-dev-box.sh --env dev --region us-east-1 --skip-image --skip-verify" in result.stdout
    assert "dry-run: the pin above was not written" in result.stdout
    assert aws.puts() == {} and "crew_model_id    =" in tfvars.read_text()


def test_release_refuses_an_ami_whose_profile_tag_differs(aws, tfvars, tmp_path):
    tags = aws.state["tags"]
    tags[AMI] = [tag if tag["Key"] != "adp:profile-revision" else {"Key": tag["Key"], "Value": "crew-other"} for tag in tags[AMI]]
    aws.set_state(tags=tags)
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--profile-revision", PROFILE, "--skip-image", "--tfvars", str(tfvars), "--pin")
    assert result.returncode == 1
    assert "tag mismatch" in result.stderr and "adp:profile-revision" in result.stderr and f"refusing to release {AMI}" in result.stderr
    assert aws.puts() == {}


def test_release_refuses_when_ssm_holds_another_profile(aws, tfvars, tmp_path):
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--profile-revision", "crew-r9", "--skip-image", "--tfvars", str(tfvars), "--pin")
    assert result.returncode == 1 and "is not the requested Crew image" in result.stderr and aws.puts() == {}


def test_release_refuses_a_foreign_account_and_missing_model(aws, tfvars, tmp_path):
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--skip-image", "--tfvars", str(tfvars), "--account-id", "000000000000")
    assert result.returncode == 2 and "not --account-id 000000000000" in result.stderr
    result = release(tmp_path, aws, "--skip-image")
    assert result.returncode == 2 and "--crew-model-id is required" in result.stderr
    aws.set_state(account="111111111111")
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--skip-image", "--tfvars", str(tfvars))
    assert result.returncode == 2 and "switch AWS_PROFILE or fix the tfvars" in result.stderr


def test_release_dry_run_without_a_published_image_plans_with_placeholders(aws, tfvars, tmp_path):
    state = aws.state
    state["ssm"] = {k: v for k, v in state["ssm"].items() if k.endswith("image-builder-project")}
    aws.set_state(ssm=state["ssm"])
    result = release(tmp_path, aws, "--crew-model-id", MODEL, "--profile-revision", PROFILE, "--tfvars", str(tfvars), "--dry-run",
                     "--bundle-dir", str(tmp_path / "bundle"))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "dry-run: not starting the bundle build" in result.stdout
    assert "override name=BUNDLE_REVISION,value=" + PROFILE in result.stdout
    assert "holds no AMI yet; the pin below uses placeholders" in result.stdout
    assert "/adp/dev/dev-box/image-id = ami-00000000" in result.stdout and aws.puts() == {}
    assert not any(call[0] == "codebuild" for call in aws.calls)
