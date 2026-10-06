"""CI jobs collecting chat ports install their locked worker dependencies."""

import shlex
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[4] / ".github" / "workflows"
WORKER = "modules/agent-factory/agent"


def job_steps(workflow, job):
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]["steps"]


@pytest.mark.parametrize(
    "workflow,job,test_step",
    [
        ("agent-control-ci.yml", "agent-control-tests", "Run delegated authorization tests"),
        ("gateway-ci.yml", "chat-ports", "Exercise chat ports through the gateway and storage emulators"),
    ],
)
def test_chat_port_jobs_install_locked_worker_dependencies_before_tests(workflow, job, test_step):
    steps = job_steps(workflow, job)
    setup = next((index for index, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-node@")), None)
    install = next(
        (
            index
            for index, step in enumerate(steps)
            if step.get("working-directory") == WORKER and shlex.split(step.get("run", "")) == ["npm", "ci", "--include=dev", "--ignore-scripts"]
        ),
        None,
    )
    execute = next(index for index, step in enumerate(steps) if step.get("name") == test_step)
    assert setup is not None, "Chat port tests require a supported Node runtime"
    assert install is not None, "Chat port tests require the worker's locked development dependencies"
    assert setup < install < execute
    assert str(steps[setup]["with"]["node-version"]) == "22"
    assert steps[setup]["with"]["cache"] == "npm"
    assert steps[setup]["with"]["cache-dependency-path"] == f"{WORKER}/package-lock.json"
    for index in (setup, install, execute):
        assert "if" not in steps[index]
        assert not steps[index].get("continue-on-error", False)


def test_agent_control_keeps_the_full_authorization_test_selection():
    steps = job_steps("agent-control-ci.yml", "agent-control-tests")
    execute = next(step for step in steps if step.get("name") == "Run delegated authorization tests")
    command = shlex.split(execute["run"].replace("\\\n", ""), comments=True)
    assert "pytest" in command
    assert "tests/agentauth/" in command
    assert "tests/orchestration/test_dispatch_pass.py" in command
    assert "-m" not in command
    # The gate may exclude exactly one thing: the chat data-path suites, which are
    # gateway storage tests the single-runner job cannot carry (~500 moto-backed
    # tests; the runner died at 30%). Nothing else may be ignored, and the
    # exclusion is only acceptable because Gateway CI runs them (checked below).
    ignored = [token for token in command if token.startswith("--ignore")]
    assert ignored == ["--ignore-glob=tests/agentauth/test_chat_*.py"]
    shard = next(step for step in job_steps("gateway-ci.yml", "test-shard") if "pytest" in str(step.get("run", "")))
    shard_command = shlex.split(shard["run"].replace("\\\n", ""), comments=True)
    assert "tests/" in shard_command, "Gateway CI shards must run the whole tree, including tests/agentauth/test_chat_*.py"
    assert not any(token.startswith("--ignore") for token in shard_command)
    # The shards may deselect only markers that another job carries: live_only
    # (live qualification) and chat_ports (the chat-ports job below).
    markers = shard_command[shard_command.index("-m") + 1]
    assert set(markers.replace("not ", "").split(" and ")) <= {"live_only", "chat_ports"}, markers
    ports = next(step for step in job_steps("gateway-ci.yml", "chat-ports") if "pytest" in str(step.get("run", "")))
    ports_command = shlex.split(ports["run"], comments=True)
    assert "tests/agentauth/" in ports_command and ports_command[ports_command.index("-m") + 1] == "chat_ports"
