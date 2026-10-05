"""Installer projection reaches the actual pinned-image native plan validator."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from harness_jobs.identity import OperationRefused

from tests.test_controller_deployment_plan import native_profile_fixture


@pytest.mark.parametrize(
    "change",
    [
        None,
        "extra-profile",
        "extra-native",
        "extra-manifest",
        "missing-network",
        "non-regional",
        "malformed-native",
        "malformed-version",
        "wrapper-digest",
        "runtime-version",
        "unresolved-artifact",
    ],
)
def test_native_batch_installer_uses_canonical_image_validation(
    monkeypatch, capsys, change
):
    # The installer is source-distributed; the API image contains the canonical
    # executor/Harness validator that VERIFY_PROGRAM imports in production.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    from installation.config import Refusal
    from installation.controller_profiles import VERIFY_PROGRAM, policy, projection

    profile = native_profile_fixture()
    profile["model_options"] = {}
    profile["serving_auth_contract"] = None
    profile["workload"].update(
        kind="batch",
        command=["python3", "/app/cuda.py"],
        args=[],
        port=None,
        auth_secret=None,
    )
    if change == "extra-profile":
        profile["unapproved"] = True
    elif change == "extra-native":
        profile["node_bootstrap"]["command"] = "unapproved"
    elif change == "extra-manifest":
        profile["node_bootstrap"]["runtime_manifest"]["unapproved"] = True
    elif change == "missing-network":
        profile.pop("network")
    elif change == "non-regional":
        profile.pop("regions")
    elif change == "malformed-native":
        profile["node_bootstrap"] = []
    elif change == "malformed-version":
        profile["node_bootstrap"]["version"] = True
    elif change == "wrapper-digest":
        profile["node_bootstrap"]["probe_wrapper_sha256"] = "unresolved"
    elif change == "runtime-version":
        profile["node_bootstrap"]["runtime_manifest"]["nodeadm_commit"] = "f" * 40
    elif change == "unresolved-artifact":
        profile["node_bootstrap"]["runtime_manifest"]["artifact_sha256"] = "0" * 64
    org_id, workspace_id = str(uuid4()), str(uuid4())
    env = {
        "org_id": org_id,
        "adp_org_id": "fixture-org",
        "controller_profiles": {
            "version": 1,
            "tenants": {
                org_id: {
                    "adp_org_id": "fixture-org",
                    "workspaces": {workspace_id: {"native-batch": profile}},
                }
            },
        },
    }
    if change in {
        "extra-profile",
        "extra-native",
        "missing-network",
        "non-regional",
        "malformed-native",
        "malformed-version",
        "wrapper-digest",
    }:
        with pytest.raises(Refusal):
            policy(env)
        if change in {"extra-native", "malformed-native", "malformed-version"}:
            # Independently check the image gate, even if presented with a
            # document that bypassed the local planning shape check.
            monkeypatch.delenv("SUPERPLANE_CONTROLLER_PROFILES_FILE", raising=False)
            monkeypatch.setenv(
                "SUPERPLANE_INSTALLATION_PROFILES",
                json.dumps(env["controller_profiles"]),
            )
            with pytest.raises(OperationRefused):
                exec(VERIFY_PROGRAM, {})
            assert capsys.readouterr().out == ""
        return
    encoded = policy(env)
    assert json.loads(encoded) == env["controller_profiles"]
    monkeypatch.delenv("SUPERPLANE_CONTROLLER_PROFILES_FILE", raising=False)
    monkeypatch.setenv("SUPERPLANE_INSTALLATION_PROFILES", encoded)
    if change is not None:
        with pytest.raises(OperationRefused):
            exec(VERIFY_PROGRAM, {})
        assert capsys.readouterr().out == ""
    else:
        exec(VERIFY_PROGRAM, {})
        evidence = json.loads(capsys.readouterr().out)
        assert evidence == {
            "sha256": projection(env)["sha256"],
            "profiles": 1,
            "validated": True,
            "workload_ready": False,
        }
