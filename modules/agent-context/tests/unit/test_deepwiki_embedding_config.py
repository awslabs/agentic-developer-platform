"""AdalFlow needs an explicit proxy URL; its default overrides SDK environment."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("namespace", "port"), [("context-test", "4000"), ("docs-test", "4400")])
def test_embedder_constructor_uses_the_deployed_proxy(namespace, port):
    # Use the same renderer as CLI and Actions, including shell-local settings
    # loaded by load_config rather than depending on an SDK environment value.
    result = subprocess.run(
        [
            "bash", "-eu", "-c",
            'source scripts/_common.sh\n'
            'NAMESPACE="$1"\nLITELLM_PORT="$2"\n'
            'template_file manifests/deepwiki-configmap.yaml\n'
            'echo "---"\n'
            'template_file manifests/deepwiki.yaml',
            "render-deepwiki", namespace, port,
        ],
        cwd=ROOT, env={"PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    resources = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    configmap = next(doc for doc in resources if doc["kind"] == "ConfigMap")
    deployment = next(doc for doc in resources if doc["kind"] == "Deployment")
    embedder = json.loads(configmap["data"]["embedder.json"])["embedder"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    sdk_url = next(entry["value"] for entry in container["env"] if entry["name"] == "OPENAI_BASE_URL")

    assert embedder["client_class"] == "OpenAIClient"
    assert embedder["initialize_kwargs"]["base_url"] == sdk_url
    assert sdk_url == f"http://litellm-proxy.{namespace}.svc.cluster.local:{port}/v1"
