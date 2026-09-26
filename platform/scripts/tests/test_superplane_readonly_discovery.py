import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "discovery", Path(__file__).parents[1] / "superplane-readonly-discovery.py"
)
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


def test_deployment_output_keeps_references_and_drops_secret_literals():
    result = discovery.sanitize(
        "deployments",
        {
            "metadata": {
                "name": "superplane-api",
                "namespace": "superplane",
                "annotations": {"last-applied": "private"},
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "api",
                                "image": "image@sha256:abc",
                                "env": [
                                    {
                                        "name": "DATABASE_URL",
                                        "value": "private-password",
                                    },
                                    {
                                        "name": "COGNITO_ISSUER",
                                        "value": "https://issuer",
                                    },
                                    {
                                        "name": "DATABASE_URL",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "superplane-db",
                                                "key": "runtime-url",
                                            }
                                        },
                                    },
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "db", "secret": {"secretName": "superplane-db"}}
                        ],
                    }
                }
            },
            "status": {"readyReplicas": 1, "conditions": [{"message": "private"}]},
        },
    )
    rendered = json.dumps(result)
    assert "private" not in rendered
    assert "https://issuer" in rendered and "superplane-db" in rendered
    assert result["status"] == {"readyReplicas": 1}


def test_service_account_never_exports_token_references_or_annotations():
    result = discovery.sanitize(
        "serviceaccounts",
        {
            "metadata": {
                "name": "api",
                "annotations": {
                    "eks.amazonaws.com/role-arn": "role",
                    "private": "private",
                },
            },
            "secrets": [{"name": "token-secret"}],
        },
    )
    assert result["role_arn"] == "role"
    assert "private" not in json.dumps(result) and "token-secret" not in json.dumps(
        result
    )


def test_missing_runner_identity_is_reported_without_exception_body(
    tmp_path, monkeypatch
):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/var/run/secrets/"):
            raise OSError("private-token-material")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    output = tmp_path / "metadata.json"
    assert discovery.run(output) == 1
    value = json.loads(output.read_text())
    assert value["errors"] == [{"type": "OSError"}]
    assert "private-token-material" not in output.read_text()
