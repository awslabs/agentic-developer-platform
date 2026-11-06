"""Executor attempts must run valid operations and never claim trusted evidence."""
import errno
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("executor_probe", ROOT / "executor_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_probe_error_is_not_network_or_permission_denial():
    def failed():
        raise OSError(errno.EIO, "synthetic I/O failure")
    with pytest.raises(OSError):
        probe.denied(failed)
    assert probe.denied(lambda: None) == {"result": "accessible"}


@pytest.mark.parametrize("case", ["privileged", "added_capability", "host_pid"])
def test_root_or_capable_process_refuses_before_an_operation(case):
    for process in ({"uid": 0, "effective_capabilities": 0, "no_new_privileges": True},
                    {"uid": 10001, "effective_capabilities": 1, "no_new_privileges": True},
                    {"uid": 10001, "effective_capabilities": 0, "no_new_privileges": False}):
        with patch.object(probe, "baseline", return_value=process), patch.object(probe, "denied") as attempt:
            result = probe.probe(case)
        assert result["probe_result"] == "unsafe_process"
        assert result["trusted_host_verified"] is False
        attempt.assert_not_called()


def test_manager_environment_is_never_printed_or_returned():
    process = {"uid": 10001, "effective_capabilities": 0, "no_new_privileges": True}
    with patch.object(probe, "baseline", return_value=process), patch.dict(probe.os.environ, {"AWS_SESSION_TOKEN": "synthetic-sensitive-canary"}):
        result = probe.probe("manager_credentials")
    assert result["probe_result"] == "credential_environment_exposed"
    assert "synthetic-sensitive-canary" not in json.dumps(result)


def test_refused_image_or_service_running_script_cannot_report_success():
    process = {"uid": 10001, "effective_capabilities": 0, "no_new_privileges": True}
    with patch.object(probe, "baseline", return_value=process):
        for case in ("arbitrary_image", "arbitrary_service"):
            assert probe.probe(case)["probe_result"] == "unexpected_script_admission"


def test_privileged_attempt_requires_host_confirmation():
    process = {"uid": 10001, "effective_capabilities": 0, "no_new_privileges": True}
    with patch.object(probe, "baseline", return_value=process), patch.object(
            probe, "mount_owned_directory", side_effect=PermissionError(errno.EPERM, "denied")):
        result = probe.probe("privileged")
    assert result["observations"] == [{"result": "inaccessible", "errno": errno.EPERM}]
    assert result["probe_result"] == "inaccessible"
    assert result["trusted_host_verified"] is False


def test_all_reviewed_cases_have_concrete_valid_gitlab_jobs():
    names = {case["name"] for case in json.loads((ROOT / "executor-negative-cases.json").read_text())["cases"]}
    pipeline = yaml.safe_load((ROOT / "executor.gitlab-ci.yml").read_text())
    assert {name.removeprefix("executor-") for name in pipeline if name.startswith("executor-")} == names
    for name in names:
        assert pipeline[f"executor-{name}"]["variables"]["ADP_EXECUTOR_CASE"] == name
        assert pipeline[f"executor-{name}"]["extends"] == ".executor"
    assert pipeline["executor-root_user"]["image"]["docker"]["user"] == "0"
    assert pipeline["executor-arbitrary_image"]["image"]["name"] == "$ADP_QUALIFICATION_HELPER_IMAGE"
    assert pipeline["executor-arbitrary_service"]["services"][0]["name"] == "$ADP_QUALIFICATION_HELPER_IMAGE"
    assert pipeline["executor-debug_trace"]["variables"]["CI_DEBUG_TRACE"] == "true"
    network_job = pipeline["network-isolation"]
    assert network_job["extends"] == ".executor"
    assert "--fixtures $ADP_QUALIFICATION_TARGETS_FILE" in " ".join(network_job["script"]).replace('"', "")
    assert "--output network-result.json" in " ".join(network_job["script"])
    assert network_job["artifacts"]["paths"] == ["network-result.json"]
    # Host modes/capabilities are actual syscall attempts, not invalid CI keywords.
    for job in pipeline.values():
        if isinstance(job, dict):
            assert not {"privileged", "network_mode", "pid", "ipc", "volumes", "cap_add", "devices"} & set(job)
