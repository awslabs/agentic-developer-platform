"""Local contract checks for the engine-owned Door deployment receipt."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/agent-context-deploy.yml"
SCRIPT = ROOT / "modules/agent-context/scripts/door_release_evidence.py"
SOURCE = "a" * 40
REVISION = "b" * 40
DIGEST = "sha256:" + "c" * 64
ACCOUNT = "000000000000"
REGION = "us-east-1"
CLUSTER = "adp-dev-eks-cluster"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-agent-context-context-mcp@{DIGEST}"


@pytest.fixture
def release_module():
    spec = importlib.util.spec_from_file_location("door_release_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment():
    return {
        "ADP_SOURCE_REVISION": SOURCE,
        "ADP_WORKFLOW_REVISION": REVISION,
        "ADP_CORRELATION": "d" * 64,
        "ACCOUNT_ID": ACCOUNT,
        "ENVIRONMENT": "dev",
        "AWS_REGION": REGION,
        "NAMESPACE": "agent-context",
        "GITHUB_REPOSITORY_ID": "1",
        "GITHUB_RUN_ID": "2",
        "GITHUB_RUN_ATTEMPT": "3",
        "ADP_INPUT_ENVIRONMENT": "dev",
        "ADP_INPUT_CUSTOMER_ACCOUNT_ID": "",
        "ADP_INPUT_CUSTOMER_AWS_LABEL": "",
        "ADP_INPUT_CUSTOMER_USER_ID": "",
    }


def readback(*, deployment_image=IMAGE, pod_digest=DIGEST, pods_present=True):
    def read(args):
        if args[:3] == ["aws", "sts", "get-caller-identity"]:
            return {"Account": ACCOUNT}
        if args[:3] == ["aws", "eks", "describe-cluster"]:
            return {
                "cluster": {
                    "name": CLUSTER,
                    "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}",
                }
            }
        if args[:3] == ["aws", "ecr", "describe-images"]:
            assert f"imageTag={SOURCE}" in args
            return {"imageDetails": [{"imageDigest": DIGEST, "imageTags": [SOURCE]}]}
        if args[:3] == ["kubectl", "get", "deployment"]:
            return {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"adp.dev/door-verification-keys": "a" * 64}},
                        "spec": {
                            "containers": [{"name": "context-mcp", "image": deployment_image}]
                        },
                    }
                }
            }
        if args[:3] == ["kubectl", "get", "pods"]:
            pod = {
                "status": {
                    "containerStatuses": [
                        {
                            "name": "context-mcp",
                            "ready": True,
                            "imageID": f"{IMAGE.split('@')[0]}@{pod_digest}",
                        }
                    ]
                }
            }
            return {"items": [pod] if pods_present else []}
        raise AssertionError(args)

    return read


def test_release_identity_matches_approved_image_and_running_pods(release_module, environment):
    context, release = release_module.evidence(environment, readback())
    assert context["source_revision"] == SOURCE
    assert context["workflow_revision"] == REVISION
    assert context["resource_id"] == f"{CLUSTER}/agent-context"
    assert context["correlation"] == environment["ADP_CORRELATION"]
    assert release["component"] == "knowledge-door"
    assert release["image_digest"] == DIGEST
    assert release["door_identity_sha256"] == "a" * 64
    assert release["run_attempt"] == 3
    assert "customer_aws_label" not in release


@pytest.mark.parametrize(
    "changes",
    [
        {"deployment_image": "unapproved:latest"},
        {"pod_digest": "sha256:" + "e" * 64},
        {"pods_present": False},
    ],
)
def test_unverified_door_image_blocks_release(release_module, environment, changes):
    with pytest.raises(ValueError, match="Door"):
        release_module.evidence(environment, readback(**changes))


def test_malformed_delivery_identity_blocks_release(release_module, environment):
    environment["ADP_SOURCE_REVISION"] = "main"
    with pytest.raises(ValueError, match="delivery identity"):
        release_module.evidence(environment, readback())


def test_workflow_guards_engine_dispatch_and_publishes_evidence_to_s3():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert {"adp_correlation", "adp_source_revision", "adp_definition_revision"} <= inputs.keys()
    deploy = workflow["jobs"]["deploy"]
    steps = {step.get("name"): step for step in deploy["steps"]}
    guard = steps["Guard engine delivery inputs"]
    assert "ACTUAL_DEFINITION" in guard["env"]
    for key in ("ADP_CORRELATION", "ADP_SOURCE", "ADP_DEFINITION"):
        assert key in guard["run"]
    checkout = next(
        step for step in deploy["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert "adp_source_revision" in checkout["with"]["ref"]
    source_guard = steps["Verify source belongs to reviewed main history"]
    assert "origin/$DEFAULT_BRANCH" in source_guard["run"]
    assert 'git fetch --no-tags origin "$GITHUB_SHA"' in source_guard["run"]
    assert "git merge-base --is-ancestor HEAD FETCH_HEAD" in source_guard["run"]
    assert "if" not in source_guard and not source_guard.get("continue-on-error", False)
    authority = next(
        step
        for step in deploy["steps"]
        if step.get("uses") == "./.github/actions/trusted-deployment"
    )
    assert deploy["steps"].index(source_guard) < deploy["steps"].index(authority)
    assert "imageTag=$ADP_DOOR_SOURCE" in steps["Deploy Context MCP Server (Door)"]["run"]
    # Evidence goes to the private S3 evidence bucket (#7829, transport #7624), not to
    # GitHub Actions artifacts whose org-wide storage quota blocked the door rollout.
    assert workflow["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert not any(step.get("uses", "").startswith("actions/upload-artifact@") for step in deploy["steps"])
    publisher = steps["Load deployment evidence publisher from workflow revision"]
    assert "modules/gateway/$file" in publisher["run"] and publisher["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"
    context = steps["Publish knowledge-door deployment context to S3"]
    assert context["run"].endswith("context agent-context-deploy.yml \"$RUNNER_TEMP/adp-deployment-context/deployment-context.json\"")
    release = steps["Publish knowledge-door release to S3"]
    assert release["run"].endswith("release knowledge-door \"$RUNNER_TEMP/adp-release-knowledge-door/release.json\"")
    for step in (publisher, context, release):
        assert "adp_correlation" in step["if"] and step["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"
    assert deploy["steps"].index(steps["Record bounded knowledge-door release and context"]) < deploy["steps"].index(publisher) < deploy["steps"].index(context) < deploy["steps"].index(release)
    assert "adp_correlation" in steps["Record bounded knowledge-door release and context"]["if"]
    for job_name in ("promote-deepwiki", "promote-ingestion"):
        assert "inputs.adp_correlation == ''" in workflow["jobs"][job_name]["if"]


@pytest.mark.parametrize(
    ("correlation", "source", "definition", "expected"),
    [
        ("", "", "", 0),
        ("d" * 64, SOURCE, REVISION, 0),
        ("d" * 64, "", REVISION, 1),
        ("invalid", SOURCE, REVISION, 1),
        # A well-formed definition pin that differs from the dispatch revision is
        # accepted here: the ancestor + byte-identical rule in "Verify source belongs
        # to reviewed main history" is what decides it (see the test below), and
        # exact equality refused every engine dispatch made after any unrelated
        # commit landed on main.
        ("d" * 64, SOURCE, SOURCE, 0),
        ("d" * 64, SOURCE, "not-a-sha", 1),
        # The engine correlates operations dispatches by run id (TLS-X02-door, 2026-10-15).
        ("orch:35c6e7f2-f43e-5c94-b30d-f405fa0016af", SOURCE, REVISION, 0),
        ("orch:not-a-run-id", SOURCE, REVISION, 1),
    ],
)
def test_dispatch_guard_accepts_complete_triplet_only(correlation, source, definition, expected):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    guard = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Guard engine delivery inputs"
    )
    environment = dict(
        os.environ,
        ADP_CORRELATION=correlation,
        ADP_SOURCE=source,
        ADP_DEFINITION=definition,
        ACTUAL_DEFINITION=REVISION,
        ADP_MANUAL_SOURCE="",
        ADP_DEEPWIKI_ONLY="false",
        ADP_INGESTION_ONLY="false",
    )
    result = subprocess.run(
        ["bash", "-e", "-c", guard], env=environment, capture_output=True, check=False
    )
    assert (result.returncode != 0) == bool(expected)


@pytest.mark.parametrize(
    "refused_command",
    [
        "",
        "rev-parse HEAD",
        f"fetch --no-tags origin {REVISION}",
        "merge-base --is-ancestor HEAD FETCH_HEAD",
        f"fetch --no-tags origin main {REVISION}",
        f"merge-base --is-ancestor {SOURCE} origin/main",
        f"merge-base --is-ancestor {REVISION} {REVISION}",
        f"show {REVISION}:.github/workflows/agent-context-deploy.yml",
    ],
)
def test_source_verification_failure_stops_before_deployment_authority(refused_command):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    guard = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Verify source belongs to reviewed main history"
    )
    git_stub = r"""
git() {
  if [[ "$*" == "$REFUSED_COMMAND" ]]; then return 1; fi
  case "$*" in
    "rev-parse HEAD") printf '%s\n' "$ADP_SOURCE" ;;
    "fetch --no-tags origin $GITHUB_SHA"|\
    "merge-base --is-ancestor HEAD FETCH_HEAD"|\
    "fetch --no-tags origin $DEFAULT_BRANCH $ADP_DEFINITION"|\
    "merge-base --is-ancestor $ADP_SOURCE origin/$DEFAULT_BRANCH"|\
    "merge-base --is-ancestor $ADP_DEFINITION $GITHUB_SHA") return 0 ;;
    "show $ADP_DEFINITION:.github/workflows/agent-context-deploy.yml") cat .github/workflows/agent-context-deploy.yml ;;
    *) return 91 ;;
  esac
}
"""
    environment = dict(
        os.environ,
        ADP_SOURCE=SOURCE,
        ADP_DEFINITION=REVISION,
        ADP_EXPECTED_DEFINITION="",
        DEFAULT_BRANCH="main",
        GITHUB_SHA=REVISION,
        REFUSED_COMMAND=refused_command,
    )
    result = subprocess.run(
        ["bash", "-e", "-c", git_stub + guard + "\nprintf 'deployment-authority-reached\\n'"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) == bool(refused_command), result.stderr
    assert ("deployment-authority-reached" in result.stdout) == (not refused_command)


def test_published_evidence_is_bounded_and_contains_verified_digest(
    release_module, environment, tmp_path, monkeypatch
):
    monkeypatch.setattr(release_module.os, "environ", {**environment, "RUNNER_TEMP": str(tmp_path)})
    reader = readback()
    monkeypatch.setattr(
        release_module.subprocess,
        "check_output",
        lambda command, timeout: json.dumps(
            reader(command[:-2] if command[0] == "aws" else command)
        ).encode(),
    )
    release_module.main()
    release = json.loads((tmp_path / "adp-release-knowledge-door/release.json").read_text())
    context = json.loads((tmp_path / "adp-deployment-context/deployment-context.json").read_text())
    assert release["image_digest"] == DIGEST
    assert context["correlation"] == environment["ADP_CORRELATION"]
    assert (tmp_path / "adp-release-knowledge-door/release.json").stat().st_size < 65536


def test_unbounded_context_does_not_publish_evidence(
    release_module, environment, tmp_path, monkeypatch
):
    environment["ADP_INPUT_CUSTOMER_AWS_LABEL"] = "x" * 65536
    monkeypatch.setattr(release_module.os, "environ", {**environment, "RUNNER_TEMP": str(tmp_path)})
    reader = readback()
    monkeypatch.setattr(
        release_module.subprocess,
        "check_output",
        lambda command, timeout: json.dumps(
            reader(command[:-2] if command[0] == "aws" else command)
        ).encode(),
    )
    with pytest.raises(ValueError, match="bounded"):
        release_module.main()
    assert not (tmp_path / "adp-release-knowledge-door/release.json").exists()


def test_deployment_keeps_door_identity_keys_annotation():
    manifest = list(
        yaml.safe_load_all((ROOT / "modules/agent-context/manifests/context-mcp.yaml").read_text())
    )
    deployment = next(
        item
        for item in manifest
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "context-mcp"
    )
    assert (
        deployment["spec"]["template"]["metadata"]["annotations"]["adp.dev/door-verification-keys"]
        == "${DOOR_VERIFICATION_KEYS_SHA256}"
    )


@pytest.mark.parametrize(
    ("correlation", "ok"),
    [
        ("orch:35c6e7f2-f43e-5c94-b30d-f405fa0016af", True),
        ("d" * 64, True),
        ("orch:not-a-run-id", False),
        ("", False),
    ],
)
def test_release_evidence_accepts_the_engine_run_id_correlation(release_module, correlation, ok):
    """The evidence record carries whatever the engine correlated the dispatch by (tools L2, 2026-10-15)."""
    assert bool(release_module.CORRELATION.fullmatch(correlation)) is ok
