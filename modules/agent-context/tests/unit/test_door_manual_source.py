"""Exercise the workflow's manual-source guards before deployment authority."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/agent-context-deploy.yml"
SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def step(name):
    return next(item for item in workflow()["jobs"]["deploy"]["steps"] if item.get("name") == name)


@pytest.mark.parametrize(
    "changes,allowed",
    [
        ({}, True),
        ({"ADP_MANUAL_SOURCE": "main"}, False),
        ({"ADP_MANUAL_SOURCE": "a" * 39}, False),
        ({"ADP_MANUAL_SOURCE": "A" * 40}, False),
        ({"ADP_CORRELATION": "c" * 64}, False),
        ({"ADP_SOURCE": SOURCE}, False),
        ({"ADP_DEFINITION": "d" * 40}, False),
        ({"ADP_DEEPWIKI_ONLY": "true"}, False),
        ({"ADP_INGESTION_ONLY": "true"}, False),
    ],
)
def test_manual_dispatch_rejects_mixed_identity_or_partial_promotion(changes, allowed):
    environment = {
        **os.environ,
        "ADP_MANUAL_SOURCE": SOURCE,
        "ADP_CORRELATION": "",
        "ADP_SOURCE": "",
        "ADP_DEFINITION": "",
        "ADP_DEEPWIKI_ONLY": "false",
        "ADP_INGESTION_ONLY": "false",
        **changes,
    }
    result = subprocess.run(
        ["bash", "-c", step("Guard engine delivery inputs")["run"]],
        env=environment, capture_output=True, text=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr


@pytest.fixture
def source_history(tmp_path):
    """Real local Git history: changed definitions and foreign commits must fail."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.DEVNULL, text=True).strip()

    git("config", "user.name", "Workflow test")
    git("config", "user.email", "workflow-test@example.invalid")
    git("remote", "add", "origin", str(origin))
    target = repo / ".github/workflows/agent-context-deploy.yml"
    target.parent.mkdir(parents=True)

    def commit(message):
        git("add", ".")
        git("commit", "-m", message)
        return git("rev-parse", "HEAD")

    target.write_text(WORKFLOW.read_text())
    source = commit("reviewed source")
    target.write_text("# different deployment authority\n")
    changed = commit("different definition")
    target.write_text(WORKFLOW.read_text())
    restored = commit("restore reviewed definition")
    (repo / "unrelated.txt").write_text("unrelated main change\n")
    current = commit("unrelated main change")
    git("checkout", "-b", "unreviewed", source)
    (repo / "foreign.txt").write_text("unreviewed branch\n")
    foreign = commit("foreign source")
    git("checkout", "main")
    git("push", "origin", "main", "unreviewed")
    return repo, git, {"source": source, "changed": changed, "restored": restored, "current": current, "foreign": foreign}


@pytest.mark.parametrize(
    "source_name,pin_name,allowed",
    [
        ("source", None, True),
        ("source", "source", True),
        ("source", "restored", True),
        ("source", "changed", False),
        ("source", "foreign", False),
        ("changed", None, False),
        ("foreign", None, False),
    ],
)
def test_actual_git_history_binds_source_and_requested_definition(source_history, source_name, pin_name, allowed):
    repo, git, revisions = source_history
    source = revisions[source_name]
    git("checkout", "--detach", source)
    result = subprocess.run(
        ["bash", "-c", step("Verify source belongs to reviewed main history")["run"] + "\nprintf 'authority-reached\\n'"],
        cwd=repo,
        env={
            **os.environ,
            "ADP_SOURCE": source,
            "ADP_DEFINITION": revisions["current"],
            "ADP_EXPECTED_DEFINITION": revisions[pin_name] if pin_name else "",
            "GITHUB_SHA": revisions["current"],
            "DEFAULT_BRANCH": "main",
        },
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    assert ("authority-reached" in result.stdout) is allowed


@pytest.mark.parametrize("digest,aws_status,allowed", [(DIGEST, 0, True), ("None", 0, False), ("latest", 0, False), ("", 1, False)])
def test_pinned_door_image_never_falls_back_to_latest(tmp_path, digest, aws_status, allowed):
    module = tmp_path / "modules/agent-context"
    (module / "scripts").mkdir(parents=True)
    (module / "scripts/_common.sh").write_text(
        'load_config() { :; }\n'
        'template_file() { printf "%s\\n" "$CONTEXT_MCP_IMAGE" >> "$TEST_IMAGES"; }\n'
    )
    (module / "scripts/deploy-door-gateway-ingress.sh").write_text("exit 0\n")
    stubs = r'''
aws() {
  printf '%s\n' "$*" >> "$TEST_AWS"
  printf '%s\n' "$TEST_DIGEST"
  return "$TEST_AWS_STATUS"
}
kubectl() { printf '%s\n' "$*" >> "$TEST_MUTATIONS"; }
python3() { printf '%064d\n' 0; }
'''
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", stubs + step("Deploy Context MCP Server (Door)")["run"]],
        cwd=tmp_path,
        env={
            **os.environ, "ADP_DOOR_SOURCE": SOURCE,
            "ECR_REGISTRY": "000000000000.dkr.ecr.us-east-1.amazonaws.com",
            "ENVIRONMENT": "dev", "AWS_REGION": "us-east-1",
            "NAMESPACE": "agent-context", "SERVICE_ACCOUNT": "context-mcp",
            "TEST_IMAGES": str(tmp_path / "images"),
            "TEST_AWS": str(tmp_path / "aws"),
            "TEST_MUTATIONS": str(tmp_path / "mutations"),
            "TEST_DIGEST": digest, "TEST_AWS_STATUS": str(aws_status),
        },
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    calls = (tmp_path / "aws").read_text().splitlines()
    assert len(calls) == 1 and f"imageTag={SOURCE}" in calls[0]
    assert (tmp_path / "mutations").exists() is allowed
    if allowed:
        images = (tmp_path / "images").read_text().splitlines()
        assert images and all(image.endswith("@" + DIGEST) for image in images)


def test_manual_selection_is_consistent_and_cannot_publish_engine_correlation():
    config = workflow()
    deploy = config["jobs"]["deploy"]
    checkout = next(item for item in deploy["steps"] if item.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == step("Verify source belongs to reviewed main history")["env"]["ADP_SOURCE"]
    assert step("Deploy Context MCP Server (Door)")["env"]["ADP_DOOR_SOURCE"] == "${{ inputs.manual_source_revision || inputs.adp_source_revision || '' }}"
    assert step("Verify source belongs to reviewed main history")["env"]["ADP_EXPECTED_DEFINITION"] == "${{ inputs.adp_definition_revision || '' }}"
    assert "inputs.manual_source_revision != ''" in deploy["if"]
    for name in ("promote-deepwiki", "promote-ingestion"):
        assert "inputs.manual_source_revision == ''" in config["jobs"][name]["if"]
    for item in deploy["steps"]:
        if item.get("name", "").startswith(("Record bounded knowledge-door", "Publish knowledge-door")):
            assert "inputs.adp_correlation != ''" in item["if"]
