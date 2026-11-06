"""CI jobs collecting chat ports install their locked worker dependencies."""

import os
import shlex
import subprocess
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
        ("agent-control-ci.yml", "agent-authority-tests", "Run delegated authorization tests"),
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
    steps = job_steps("agent-control-ci.yml", "agent-authority-tests")
    execute = next(step for step in steps if step.get("name") == "Run delegated authorization tests")
    command = shlex.split(execute["run"].replace("\\\n", ""), comments=True)
    assert "pytest" in command
    command = command[command.index("pytest") :]
    assert "tests/agentauth/" in command
    assert "tests/orchestration/test_dispatch_pass.py" in command
    assert "-m" not in command
    # Chat data-path suites run in Gateway CI. Four authority files run once
    # under the mandatory control coverage job; they must not disappear when
    # avoiding duplicate execution in the authority shards.
    covered_elsewhere = {
        "tests/agentauth/test_exit_retention.py",
        "tests/agentauth/test_retained_abort_recovery.py",
        "tests/agentauth/test_abort_reconciliation.py",
        "tests/agentauth/test_human_control.py",
    }
    ignored = {token for token in command if token.startswith("--ignore")}
    assert ignored == {"--ignore-glob=tests/agentauth/test_chat_*.py", *(f"--ignore={path}" for path in covered_elsewhere)}
    coverage_commands = []
    for step in job_steps("agent-control-ci.yml", "control-contract-tests"):
        for line in step.get("run", "").replace(chr(92) + chr(10), "").splitlines():
            if line.strip().startswith("pytest ") and "--cov-branch" in line:
                coverage_commands.append(shlex.split(line))
    for path in covered_elsewhere:
        matching = [args for args in coverage_commands if path in args]
        assert len(matching) == 1, f"{path} must execute once under the mandatory coverage gate"
        assert not any(arg.startswith(("--ignore", "-k", "-m")) for arg in matching[0])
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


@pytest.mark.parametrize(
    "contracts,authority",
    [
        ("success", "success"),
        ("failure", "success"),
        ("success", "failure"),
        ("skipped", "success"),
        ("success", "skipped"),
        ("success", "cancelled"),
        ("", "success"),
    ],
)
def test_stable_control_gate_requires_contracts_coverage_and_all_shards(contracts, authority):
    jobs = yaml.safe_load((WORKFLOWS / "agent-control-ci.yml").read_text())["jobs"]
    gate = jobs["agent-control-tests"]
    assert gate["name"] == "Agent control tests"
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {"control-contract-tests", "agent-authority-tests"}
    assert jobs["agent-authority-tests"]["strategy"]["fail-fast"] is False
    result = subprocess.run(["bash", "-e", "-c", gate["steps"][0]["run"]], env={**os.environ, "CONTRACTS": contracts, "AUTHORITY": authority})
    assert (result.returncode == 0) == (contracts == authority == "success")
    steps = jobs["control-contract-tests"]["steps"]
    assert any(step.get("name") == "Enforce abort recovery coverage per module" for step in steps)
    assert any(step.get("name") == "Enforce control-module coverage (>=85% line and branch)" for step in steps)


@pytest.mark.parametrize(
    "results",
    [("success", "success", "success")]
    + [
        tuple(status if index == failing else "success" for index in range(3))
        for failing in range(3)
        for status in ("failure", "skipped", "cancelled")
    ],
)
def test_build_gate_preserves_test_prerequisites_while_smoke_build_runs_early(results):
    jobs = yaml.safe_load((WORKFLOWS / "gateway-ci.yml").read_text())["jobs"]
    gate = jobs["build-result"]
    assert gate["name"] == "Build Container"
    assert gate["if"] == "${{ always() && !inputs.offline_only }}"
    assert set(gate["needs"]) == {"build", "test-shard", "frontend-test"}
    assert "needs" not in jobs["build"]
    result = subprocess.run(
        ["bash", "-e", "-c", gate["steps"][0]["run"]],
        env={**os.environ, **dict(zip(("SMOKE", "TESTS", "FRONTEND"), results, strict=True))},
    )
    assert (result.returncode == 0) == all(status == "success" for status in results)
