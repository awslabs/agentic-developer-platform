"""Exercise real entrypoint processes with isolated Git and executable tool mocks."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
BUILD = "modules/agent-factory/scripts/build-and-push.sh"
DEPLOY = "modules/harness/mcp-hub/scripts/deploy-agent-mail.sh"
PYTHON = "modules/harness/mcp-hub/scripts/deploy_agent_mail.py"
CONTEXT = "modules/harness/mcp-hub/docker/agent-mail"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
DIGEST = "sha256:" + "a" * 64
URI = REGISTRY + "/mcp-agent-mail@" + DIGEST


@pytest.fixture
def sandbox(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in (BUILD, DEPLOY, PYTHON):
        dest = repo / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, dest)
    context = repo / CONTEXT
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n")
    (context / "constraints.txt").write_text("frozen dependency input\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    (context / "constraints.txt").write_text("uncommitted input must never build\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    mock = """#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps([name,*args])+'\\n')
if name=='aws':
 if args[1]=='describe-repositories':print(os.getenv('MUTABILITY','IMMUTABLE'))
 elif args[1]=='describe-images':
  state=Path(os.environ['STATE'])
  if os.getenv('LOOKUP_ERROR'):
   print(os.environ['LOOKUP_ERROR'],file=sys.stderr);sys.exit(1)
  if not state.exists() and not os.getenv('EXISTING'):
   state.touch();print('ImageNotFoundException',file=sys.stderr);sys.exit(1)
  print(os.getenv('DIGEST','sha256:'+'a'*64))
 elif args[1]=='get-login-password':print('synthetic-login')
 else:sys.exit(81)
elif name=='docker':
 if args[0]=='login':sys.stdin.read()
 elif args[0]=='build':
  context=Path(args[-1])
  assert context.joinpath('constraints.txt').read_text()=='frozen dependency input\\n'
  assert Path(args[args.index('-f')+1])==context/'Dockerfile'
  assert '--pull' in args and '--no-cache' in args
 elif args[0]!='push':sys.exit(82)
elif name=='kubectl':
 if 'get' in args:
  if os.getenv('MISSING_SECRET'):sys.exit(1)
  print('secret/agent-mail-auth')
 elif 'apply' in args:Path(os.environ['APPLIED']).write_text(sys.stdin.read())
 elif 'rollout' not in args:sys.exit(83)
"""
    for name in ("aws", "docker", "kubectl"):
        target = binaries / name
        target.write_text(mock)
        target.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(binaries) + os.pathsep + os.environ["PATH"],
        CALLS=str(tmp_path / "calls"),
        STATE=str(tmp_path / "state"),
        APPLIED=str(tmp_path / "applied"),
        ECR_REGISTRY=REGISTRY,
        AWS_REGION="us-east-1",
    )
    for key in (
        "IMAGE_TAG",
        "ADP_SOURCE_SHA",
        "AGENT_MAIL_IMAGE",
        "AGENT_MAIL_KUBE_CONTEXT",
        "AGENT_MAIL_MANIFESTS_DIR",
        "PUBLISH_LATEST",
    ):
        env.pop(key, None)
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for name, kind in (
        ("namespace", "Namespace"),
        ("serviceaccount", "ServiceAccount"),
        ("rbac", "Role"),
        ("pvc", "PersistentVolumeClaim"),
        ("configmap", "ConfigMap"),
        ("service", "Service"),
        ("ingress", "Ingress"),
    ):
        (manifests / (name + ".yaml")).write_text(
            f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: agent-mail\n"
        )
    (manifests / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: agent-mail\nspec:\n  template:\n    spec:\n      containers:\n      - name: agent-mail\n        image: ${AGENT_MAIL_IMAGE}\n"
    )
    return repo, env, sha, manifests


def run(sandbox, script, *args, **updates):
    repo, env, _, _ = sandbox
    return subprocess.run(
        ["bash", str(repo / script), *args],
        cwd="/",
        env=dict(env, **updates),
        capture_output=True,
        text=True,
        check=False,
    )


def calls(sandbox):
    path = Path(sandbox[1]["CALLS"])
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def test_publish_archives_full_revision_from_any_cwd(sandbox):
    result = run(sandbox, BUILD)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == URI
    log = calls(sandbox)
    push = next(c for c in log if c[:2] == ["docker", "push"])
    assert push[-1] == REGISTRY + "/mcp-agent-mail:" + sandbox[2]
    assert not any("latest" in arg for c in log for arg in c)
    assert not any("create-repository" in c for c in log)


def test_existing_immutable_tag_is_reused_without_docker(sandbox):
    result = run(sandbox, BUILD, EXISTING="1")
    assert result.returncode == 0 and result.stdout.strip() == URI
    assert all(c[0] != "docker" for c in calls(sandbox))


@pytest.mark.parametrize(
    "updates",
    [
        {"MUTABILITY": "MUTABLE"},
        {"MUTABILITY": "IMMUTABLE_WITH_EXCLUSION"},
        {"LOOKUP_ERROR": "AccessDeniedException"},
        {"DIGEST": "None", "EXISTING": "1"},
        {"IMAGE_TAG": "latest"},
        {"ADP_SOURCE_SHA": "abcd"},
        {"AWS_REGION": "eu-west-1"},
    ],
)
def test_publisher_rejects_unsafe_or_ambiguous_inputs(sandbox, updates):
    assert run(sandbox, BUILD, **updates).returncode != 0
    assert all(c[0] != "docker" for c in calls(sandbox))


@pytest.mark.parametrize("build", [False, True])
def test_dry_run_has_no_cloud_docker_or_kubernetes_calls(sandbox, build):
    args = ["--dry-run", "--manifests-dir", str(sandbox[3]), "--image", URI]
    if build:
        args.append("--build")
    result = run(sandbox, DEPLOY, *args)
    assert result.returncode == 0, result.stderr
    assert URI in result.stdout
    assert calls(sandbox) == []


def test_deploy_build_resolves_helper_and_applies_digest_without_rotating_secret(
    sandbox,
):
    result = run(
        sandbox,
        DEPLOY,
        "--build",
        "--manifests-dir",
        str(sandbox[3]),
        "--context",
        "explicit-test-context",
    )
    assert result.returncode == 0, result.stderr
    log = calls(sandbox)
    assert log[0] == [
        "kubectl",
        "--context",
        "explicit-test-context",
        "get",
        "secret",
        "agent-mail-auth",
        "-n",
        "agent-mail",
        "-o",
        "name",
    ]
    assert URI in Path(sandbox[1]["APPLIED"]).read_text()
    assert not any("create" in c or "update-kubeconfig" in c for c in log)
    assert all(
        c[1:3] == ["--context", "explicit-test-context"]
        for c in log
        if c[0] == "kubectl"
    )


@pytest.mark.parametrize(
    "failure",
    [
        "missing-secret",
        "missing-manifest",
        "missing-context",
        "mutable-image",
        "secret-manifest",
    ],
)
def test_deploy_refuses_before_any_external_write(sandbox, failure):
    args = ["--manifests-dir", str(sandbox[3]), "--context", "test", "--image", URI]
    updates = {}
    if failure == "missing-secret":
        updates["MISSING_SECRET"] = "1"
    elif failure == "missing-manifest":
        (sandbox[3] / "pvc.yaml").unlink()
    elif failure == "missing-context":
        args[2:4] = []
    elif failure == "mutable-image":
        args[-1] = REGISTRY + "/mcp-agent-mail:latest"
    else:
        (sandbox[3] / "configmap.yaml").write_text("kind: Secret\n")
    assert run(sandbox, DEPLOY, *args, **updates).returncode != 0
    assert all(c[0] == "kubectl" and "get" in c for c in calls(sandbox))


def test_skip_ingress_does_not_require_ingress_file(sandbox):
    (sandbox[3] / "ingress.yaml").unlink()
    result = run(
        sandbox,
        DEPLOY,
        "--dry-run",
        "--skip-ingress",
        "--manifests-dir",
        str(sandbox[3]),
        "--image",
        URI,
    )
    assert result.returncode == 0, result.stderr
    assert "kind: Ingress" not in result.stdout
    assert calls(sandbox) == []


def test_build_dry_run_without_digest_validates_but_does_not_claim_rendered_image(
    sandbox,
):
    result = run(
        sandbox, DEPLOY, "--dry-run", "--build", "--manifests-dir", str(sandbox[3])
    )
    assert result.returncode == 0, result.stderr
    assert "awaits the published digest" in result.stdout
    assert "DIGEST_RESOLVED_AFTER_PUBLICATION" not in result.stdout
    assert calls(sandbox) == []


@pytest.mark.parametrize(
    "body",
    [
        'kind: "Secret"\n',
        "kind: List\nitems:\n- kind: Secret\n",
        "kind: ConfigMap\n---\nkind: Secret\n",
    ],
)
def test_secret_cannot_be_hidden_by_yaml_encoding_or_document_wrapper(sandbox, body):
    (sandbox[3] / "configmap.yaml").write_text(body)
    result = run(
        sandbox, DEPLOY, "--dry-run", "--manifests-dir", str(sandbox[3]), "--image", URI
    )
    assert result.returncode != 0
    assert calls(sandbox) == []


def test_bad_post_publish_digest_prevents_kubernetes_apply(sandbox):
    result = run(
        sandbox,
        DEPLOY,
        "--build",
        "--manifests-dir",
        str(sandbox[3]),
        "--context",
        "test",
        DIGEST="None",
    )
    assert result.returncode != 0
    assert not any(c[0] == "kubectl" and "apply" in c for c in calls(sandbox))


def test_missing_operator_manifests_fails_before_cloud_calls(sandbox):
    result = run(sandbox, DEPLOY, "--build", "--context", "test")
    assert result.returncode != 0
    assert calls(sandbox) == []
