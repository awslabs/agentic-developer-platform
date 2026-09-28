"""An image update must not replace the installed authentication transport."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "skypilot_release",
    Path(__file__).resolve().parents[1] / "infra/scripts/skypilot_release.py",
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def installed():
    return {
        "metadata": {"labels": {"adp.aws-e.io/installation": "installation-id"}},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "skypilot-api",
                            "command": ["python3", "/skypilot-bootstrap/bootstrap.py"],
                            "args": ["--host=127.0.0.1", "--port=46580"],
                            "readinessProbe": {"exec": {"command": ["health"]}},
                        },
                        {
                            "name": "authenticated-transport",
                            "command": ["python", "-m", "app.skypilot_proxy"],
                        },
                    ]
                }
            }
        },
    }


def test_unknown_installed_runtime_is_rejected():
    deployment = installed()
    deployment["spec"]["template"]["spec"]["containers"][0]["args"] = ["--host=0.0.0.0"]
    with pytest.raises(ValueError, match="Unrecognized"):
        release.image_only(deployment)


def test_absent_deployment_uses_manifest_lane():
    assert not release.image_only({})


def test_installed_runtime_uses_image_only_lane(monkeypatch):
    def run(command, **kwargs):
        assert command[:3] == ["kubectl", "get", "deployment"]
        return SimpleNamespace(stdout=json.dumps(installed()))

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr("sys.argv", ["release", "inspect", "--namespace", "skypilot"])
    assert release.image_only(installed())
    release.main()


def test_standalone_runtime_uses_manifest_lane():
    deployment = installed()
    deployment["metadata"]["labels"] = {}
    deployment["spec"]["template"]["spec"]["containers"].pop()
    assert not release.image_only(deployment)
