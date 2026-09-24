"""Parse real TLS material while refusing fallback, exec plugins and foreign scopes."""

import base64
import ssl
from types import SimpleNamespace

import pytest
import yaml
from harness_jobs.identity import OperationRefused

from superplane_executor.workspace import Workspace


@pytest.mark.parametrize(
    "mutation",
    [None, "exec", "token_file", "management", "namespace", "context", "tls"],
)
def test_exact_workspace_credentials_are_required(tmp_path, mutation):
    workspace_id = "10000000-0000-0000-0000-000000000001"
    arn = "arn:aws:eks:us-east-1:123456789012:cluster/workspace"
    target = {
        "namespace": "tenant-a",
        "endpoint": "https://workspace.example",
        "cluster_arn": arn,
    }
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=SimpleNamespace(workspace_id=workspace_id))
    )
    certificate = ssl.DER_cert_to_PEM_cert(
        ssl.create_default_context().get_ca_certs(binary_form=True)[0]
    )
    data = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": arn,
        "contexts": [
            {
                "name": arn,
                "context": {
                    "cluster": "workspace",
                    "user": "scoped",
                    "namespace": "tenant-a",
                },
            }
        ],
        "clusters": [
            {
                "name": "workspace",
                "cluster": {
                    "server": target["endpoint"],
                    "certificate-authority-data": base64.b64encode(
                        certificate.encode()
                    ).decode(),
                },
            }
        ],
        "users": [{"name": "scoped", "user": {"token": "scoped-workspace-token"}}],
    }
    if mutation == "exec":
        data["users"][0]["user"] = {"exec": {"command": "must-never-run"}}
    elif mutation == "token_file":
        data["users"][0]["user"]["tokenFile"] = "/ambient/token"
    elif mutation == "management":
        target["endpoint"] = data["clusters"][0]["cluster"]["server"] = (
            "https://management.example"
        )
    elif mutation == "namespace":
        data["contexts"][0]["context"]["namespace"] = "foreign"
    elif mutation == "context":
        data["current-context"] = "ambient"
    elif mutation == "tls":
        data["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    (tmp_path / (workspace_id + ".kubeconfig")).write_text(yaml.safe_dump(data))
    client = Workspace(tmp_path, "https://management.example")
    if mutation:
        with pytest.raises(OperationRefused):
            client.credentials(operation, target)
    else:
        token, tls = client.credentials(operation, target)
        assert (
            token == "scoped-workspace-token" and tls.verify_mode == ssl.CERT_REQUIRED
        )
        assert tls.check_hostname
