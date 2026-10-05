"""Only the trusted Door and search server may receive the backend credential."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ensure_zoekt_auth", ROOT / "scripts/ensure-zoekt-auth.py"
)
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


def test_secret_is_private_to_the_door_and_search_server():
    recipients = []
    for path in (ROOT / "manifests").glob("*.yaml"):
        for resource in yaml.safe_load_all(path.read_text()):
            if not isinstance(resource, dict):
                continue
            pod = resource.get("spec", {}).get("template", {}).get("spec", {})
            for container in pod.get("containers", []):
                for env in container.get("env", []):
                    secret = env.get("valueFrom", {}).get("secretKeyRef", {})
                    if secret.get("name") == "zoekt-backend-auth":
                        assert not secret.get("optional", False)
                        recipients.append(container["name"])
    assert sorted(recipients) == ["context-mcp", "zoekt-webserver"]


def test_existing_key_is_not_rotated_on_redeploy(monkeypatch):
    existing = {"data": {"api-key": base64.b64encode(b"k" * 64).decode()}}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(existing), returncode=0)

    monkeypatch.setattr(provision.subprocess, "run", run)
    provision.ensure_key("context")
    assert len(calls) == 1
    assert calls[0][1] == "get"


def test_new_key_is_generated_and_sent_through_stdin(monkeypatch):
    stored = None

    def run(args, **kwargs):
        nonlocal stored
        if args[1] == "create":
            stored = json.loads(kwargs["input"])
            assert len(base64.b64decode(stored["data"]["api-key"])) >= 32
            assert stored["metadata"] == {"name": "zoekt-backend-auth", "namespace": "context"}
            assert args == ["kubectl", "create", "-f", "-"]
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout=json.dumps(stored) if stored else "", returncode=0)

    monkeypatch.setattr(provision.subprocess, "run", run)
    provision.ensure_key("context")
    assert stored is not None


def test_invalid_existing_key_refuses_deployment(monkeypatch):
    existing = {
        "data": {"api-key": base64.b64encode(b"PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND").decode()}
    }
    monkeypatch.setattr(
        provision.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(existing), returncode=0),
    )
    with pytest.raises(RuntimeError, match="invalid"):
        provision.ensure_key("context")
