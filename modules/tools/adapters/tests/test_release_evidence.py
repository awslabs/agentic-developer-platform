"""Exercise the actual observer, renderer and release document producer together."""

import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_runtime_evidence as runtime_fixtures

ROOT = Path(__file__).parents[1]


@pytest.fixture
def cluster(configuration, request):
    return runtime_fixtures.cluster.__wrapped__(configuration, request)


@pytest.fixture
def producer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("adapter_release_evidence", ROOT / "release_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def document_inputs(configuration, cluster):
    env = {
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "d" * 40,
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "example-cluster",
        "ADP_TOOLS_ADAPTER_NAMESPACE": "adp-tools",
        "ADP_TOOLS_ADAPTER_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-tool-adapters",
        "ADP_CORRELATION": "e" * 64,
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_REPOSITORY_ID": "17",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": "org/repo/.github/workflows/tools-platform-deploy.yml@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": "d" * 40,
    }
    publication = {"source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64}
    observation = {
        **runtime_fixtures.observe(cluster),
        "account_id": "123456789012",
        "region": "us-east-1",
        "cluster": "example-cluster",
        "publication": copy.deepcopy(publication),
    }
    inputs = {
        "environment": "dev",
        "mode": "release",
        "manual_source_revision": "",
        "stage": "tool-adapters",
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "d" * 40,
        "adp_correlation": "e" * 64,
    }
    return SimpleNamespace(
        env=env, configuration=configuration, publication=publication, observation=observation, inputs=inputs
    )


def produce(producer, state):
    return producer.documents(state.env, state.configuration, state.publication, state.observation, state.inputs)


def test_actual_observation_becomes_bounded_release_and_context(producer, document_inputs, cluster):
    release, context = produce(producer, document_inputs)
    assert release["adapter_manifest"] == cluster.expected
    assert release["compatibility"] == {
        "adapter": {
            "release": document_inputs.configuration.release.model_dump(mode="json"),
            "mode": "foundation",
            "contract": release["adapter_contract"],
            "configuration_sha256": document_inputs.observation["configuration_sha256"],
            "manifest_sha256": release["assets"]["adapter-manifest.json"],
        }
    }
    for name, field in (("adapter-manifest.json", "adapter_manifest"), ("adapter-contract.json", "adapter_contract")):
        assert release["assets"][name] == hashlib.sha256(producer.encoded(release[field])).hexdigest()
    assert release["component"] == "tool-adapters"
    assert release["resource_id"] == context["resource_id"] == "example-cluster/adp-tools"
    assert context["workflow_path"] == ".github/workflows/tools-platform-deploy.yml"
    assert context["inputs"] == {
        "environment": "dev",
        "mode": "release",
        "manual_source_revision": "",
        "stage": "tool-adapters",
    }
    assert all(len(producer.encoded(value)) < 65536 for value in (release, context))
    assert "verified" not in release and "pods" not in release


def test_compatibility_pins_refuse_unobserved_configuration(producer, document_inputs):
    state = document_inputs
    state.observation["configuration_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="observation_mismatch"):
        produce(producer, state)

    state.observation["configuration_sha256"] = hashlib.sha256(
        state.configuration.model_dump_json().encode()
    ).hexdigest()
    state.observation["contract"]["schema_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="observation_mismatch"):
        produce(producer, state)


def test_identity_context_uses_accepted_generic_target_and_transport_digest(producer, document_inputs):
    state = document_inputs
    state.env.update(ADP_IDENTITY_CONTRACT_DIGEST="f" * 64)
    state.inputs.update(mode="identity", adp_contract_digest="f" * 64)
    release, context = produce(producer, state)
    assert release["resource_id"] == context["resource_id"] == "example-cluster/adp-tools"
    assert context["resource_kind"] == "eks-namespace"
    assert context["inputs"]["adp_contract_digest"] == "f" * 64
    assert "identity_gateway_revision" not in context["inputs"]
    state.inputs["adp_contract_digest"] = "e" * 64
    with pytest.raises(ValueError):
        produce(producer, state)


def test_identity_release_accepts_only_byte_identical_ancestor_definition(
    producer, document_inputs, tmp_path, monkeypatch
):
    import subprocess

    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / ".github/workflows/tools-platform-deploy.yml"
    workflow.parent.mkdir(parents=True)

    def git(*arguments):
        return subprocess.check_output(["git", *arguments], text=True).strip()

    git("init", "-b", "main")
    workflow.write_text("name: identity\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "approved")
    approved = git("rev-parse", "HEAD")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "later")
    actual = git("rev-parse", "HEAD")
    state = document_inputs
    state.inputs.update(mode="identity", adp_contract_digest="f" * 64, adp_definition_revision=approved)
    state.env.update(ADP_IDENTITY_CONTRACT_DIGEST="f" * 64, ADP_DEFINITION_REVISION=actual, GITHUB_WORKFLOW_SHA=actual)
    assert produce(producer, state)[1]["workflow_revision"] == actual
    workflow.write_text("name: changed\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "changed")
    state.env.update(ADP_DEFINITION_REVISION=git("rev-parse", "HEAD"), GITHUB_WORKFLOW_SHA=git("rev-parse", "HEAD"))
    with pytest.raises(ValueError):
        produce(producer, state)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("env", "GITHUB_WORKFLOW_SHA", "f" * 40),
        ("env", "GITHUB_WORKFLOW_REF", "org/repo/.github/workflows/other.yml@refs/heads/main"),
        ("env", "GITHUB_EVENT_NAME", "pull_request"),
        ("env", "GITHUB_RUN_ATTEMPT", "2"),
        ("env", "ADP_TOOLS_ACCOUNT_ID", "111122223333"),
        ("env", "ADP_TOOLS_EKS_CLUSTER_NAME", "different-cluster"),
        ("publication", "image_digest", "sha256:" + "f" * 64),
        ("observation", "namespace", "different-namespace"),
        ("observation", "configuration_sha256", "f" * 64),
        ("observation", "ready_replicas", 1),
        ("observation", "execution_enabled", True),
        ("observation", "contract", {"schema_sha256": "f" * 64}),
        ("inputs", "environment", "prod"),
        ("inputs", "mode", "drift"),
        ("inputs", "adp_source_revision", "f" * 40),
    ],
)
def test_mismatched_deployment_cannot_produce_receipt(producer, document_inputs, section, key, value):
    getattr(document_inputs, section)[key] = value
    with pytest.raises(ValueError):
        produce(producer, document_inputs)


def test_cli_writes_new_private_documents_only(producer, document_inputs, tmp_path, monkeypatch):
    state = document_inputs
    for suffix, value, key in (
        ("config", state.configuration.model_dump(mode="json"), "ADP_TOOLS_ADAPTER_CONFIG_FILE"),
        ("publication", state.publication, "ADP_TOOLS_ADAPTER_PUBLICATION_FILE"),
        ("observation", state.observation, "ADP_TOOLS_ADAPTER_EVIDENCE_FILE"),
    ):
        path = tmp_path / (suffix + ".json")
        path.write_text(json.dumps(value))
        monkeypatch.setenv(key, str(path))
    for name, value in state.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON", json.dumps(state.inputs))
    output = tmp_path / "receipt"
    monkeypatch.setattr(sys, "argv", ["release_evidence.py", "--output", str(output)])
    producer.main()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name in ("release.json", "deployment-context.json"):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
        assert json.loads((output / name).read_text())["source_revision"] == "a" * 40
    with pytest.raises(FileExistsError):
        producer.main()


def test_identity_original_context_then_native_receipt_use_actual_workflow_paths(
    producer, document_inputs, tmp_path, monkeypatch
):
    import identity_dispatch_context as dispatch

    state = document_inputs
    state.inputs.update(mode="identity", adp_contract_digest="f" * 64)
    state.env.update(ADP_IDENTITY_CONTRACT_DIGEST="f" * 64)
    for name, value in state.env.items():
        monkeypatch.setenv(name, value)
    for suffix, value, key in (
        ("config", state.configuration.model_dump(mode="json"), "ADP_TOOLS_ADAPTER_CONFIG_FILE"),
        ("publication", state.publication, "ADP_TOOLS_ADAPTER_PUBLICATION_FILE"),
        ("observation", state.observation, "ADP_TOOLS_ADAPTER_EVIDENCE_FILE"),
    ):
        path = tmp_path / (suffix + ".json")
        path.write_text(json.dumps(value))
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON", json.dumps(state.inputs))
    output = tmp_path / "release"
    monkeypatch.setenv("ADP_TOOLS_ADAPTER_RELEASE_DIR", str(output))

    class Deployment:
        def __init__(self, env):
            self.account = env["ADP_TOOLS_ACCOUNT_ID"]
            self.source = env["ADP_SOURCE_REVISION"]
            self.role = f"arn:aws:iam::{self.account}:role/test-deployment"

        def aws(self, *args):
            assert args == ("sts", "get-caller-identity")
            return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/test-deployment/run"}

        def run(self, args):
            if args == ["git", "rev-parse", "HEAD"]:
                return self.source
            if args[:2] == ["git", "show"]:
                return "name: identity\n"
            assert args[:3] == ["git", "merge-base", "--is-ancestor"]
            return ""

    monkeypatch.setattr(dispatch, "Deployment", Deployment)
    command = (ROOT / "deploy.sh").read_text().split("  dispatch-context)\n", 1)[1].split(";;", 1)[0]
    path_expression = re.search(r'--output "([^"]+)"', command).group(1)
    workflow = (ROOT.parents[2] / ".github/workflows/tools-platform-deploy.yml").read_text()
    publisher = workflow.split("- name: Publish signed original dispatch context\n", 1)[1].split("- name:", 1)[0]
    assert f'"{path_expression}"' in publisher
    original_path = Path(os.path.expandvars(path_expression))
    monkeypatch.setattr(sys, "argv", ["identity_dispatch_context.py", "--output", str(original_path)])
    dispatch.main()
    original = original_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["release_evidence.py", "--output", str(output)])
    producer.main()
    assert original_path.read_bytes() == original
    assert json.loads(original) == json.loads((output / "deployment-context.json").read_bytes())
    assert stat.S_IMODE(original_path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        producer.main()


def test_input_links_duplicates_and_oversize_are_refused(producer, tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"source_revision":"a","source_revision":"b"}')
    with pytest.raises(ValueError, match="duplicate_field"):
        producer.read_document(path)
    path.write_text(" " * 65537)
    with pytest.raises(ValueError, match="input_size"):
        producer.read_document(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular_input_required"):
        producer.read_document(link)


def test_tools_ci_qualifies_composed_terraform_root():
    workflow = (Path(__file__).resolve().parents[4] / ".github/workflows/tools-ci.yml").read_text()
    qualification = workflow.split("- name: Verify adapter Terraform with mocked providers", 1)[1].split(
        "\n      - name:", 1
    )[0]
    assert "modules/tools/adapters/infra" in qualification
    assert "modules/tools/adapters/foundation-infra" in qualification
    assert "modules/tools/adapters/deployment-infra" in qualification
    assert "AWS_EC2_METADATA_DISABLED: 'true'" in qualification
    assert 'terraform -chdir="$module" test -no-color' in qualification
