"""The published broker schema is intentionally narrower than internal routes."""

from pathlib import Path

import yaml


SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "credentials.yaml"


def test_credential_target_only_advertises_consent_bound_operations():
    schema = yaml.safe_load(SCHEMA.read_text())
    assert schema["openapi"].startswith("3.")
    assert schema["servers"] == [{"url": "https://broker.example.invalid"}]
    operations = {
        (path, method): operation["operationId"]
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }
    assert operations == {
        ("/internal/v1/user-credentials", "get"): "list",
        ("/internal/v1/proxy-request", "post"): "proxy_request",
        ("/internal/v1/credential-assume-role", "post"): "assume_role",
        ("/internal/v1/credential-materialize", "post"): "materialize",
    }
    assert "raw-read" not in SCHEMA.read_text().lower()
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            assert "invocation_id" not in str(operation)
            if path == "/internal/v1/user-credentials":
                assert {parameter["name"] for parameter in operation["parameters"]} == {"service"}
            else:
                request = operation["requestBody"]["content"]["application/json"]["schema"]
                assert not {"user_id", "agent_id", "task_id"} & set(request.get("required", []))
                assert not {"user_id", "agent_id", "task_id"} & set(request["properties"])
