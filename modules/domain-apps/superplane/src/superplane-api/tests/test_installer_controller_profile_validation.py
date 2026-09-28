"""Installer probe uses the actual maintained builder in the API environment."""

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.test_controller_deployment_plan import profile_fixture


@pytest.mark.parametrize(
    "failure", [None, "image", "auth", "replicas", "certificate", "extra"]
)
@pytest.mark.parametrize("installed", [False, True])
def test_pinned_image_profile_validation_uses_actual_closed_plan_contract(
    tmp_path, monkeypatch, failure, installed
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3]))
    from installation.controller_profiles import VERIFY_PROGRAM

    profile = profile_fixture(uuid4())
    if failure == "image":
        profile["workload"]["image"] = "registry.example/model:latest"
    elif failure == "auth":
        profile["workload"]["auth_secret"] = ""
    elif failure == "replicas":
        profile["model_options"]["replicas"] = 2
    elif failure == "certificate":
        profile["certificate_authority"] = "unverified"
    elif failure == "extra":
        profile["default_credentials"] = True
    document = {
        "version": 1,
        "tenants": {
            str(uuid4()): {
                "adp_org_id": "adp-test",
                "workspaces": {str(uuid4()): {"approved-model": profile}},
            }
        },
    }
    encoded = json.dumps(document)
    environment = dict(os.environ)
    environment.pop("SUPERPLANE_CONTROLLER_PROFILES_FILE", None)
    if installed:
        path = tmp_path / "profiles.json"
        path.write_text(encoded)
        environment["SUPERPLANE_CONTROLLER_PROFILES_FILE"] = str(path)
    else:
        environment["SUPERPLANE_INSTALLATION_PROFILES"] = encoded
    result = subprocess.run(
        [sys.executable, "-c", VERIFY_PROGRAM],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if failure is None:
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed["validated"] is True and observed["profiles"] == 1
        assert observed["workload_ready"] is False
    else:
        assert result.returncode != 0
        assert not result.stdout.strip()
