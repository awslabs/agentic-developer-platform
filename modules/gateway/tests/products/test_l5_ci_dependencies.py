"""Gateway shards install the packages required by mounted L5 tests."""

import shlex
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def test_gateway_shards_install_l5_before_collecting_tests():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-ci.yml").read_text())
    steps = workflow["jobs"]["test-shard"]["steps"]
    prepare_index, prepare = next((index, step) for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/setup-gateway-python")
    test_index = next(index for index, step in enumerate(steps) if step.get("name") == "Run Tests")
    action = yaml.safe_load((ROOT / prepare["uses"] / "action.yml").read_text())
    install = next(step for step in action["runs"]["steps"] if step.get("name") == "Install or refresh dependencies")
    # Both a cache miss and a cache hit must bind the mounted local packages.
    commands = [shlex.split(line) for line in install["run"].splitlines() if " -m pip install " in line and " -e " in line]
    assert len(commands) == 2
    for command in commands:
        assert {"modules/gateway[dev]", "modules/L5-services[dev]"} <= set(command)
        packages = {(ROOT / command[index + 1].split("[", 1)[0]).resolve() for index, token in enumerate(command) if token == "-e"}
        assert {ROOT / "modules/gateway", ROOT / "modules/L5-services"} <= packages
    assert prepare_index < test_index
    for step in (prepare, install):
        assert "if" not in step
        assert not step.get("continue-on-error", False)


def test_l5_archive_builder_is_a_pinned_test_dependency():
    package = tomllib.loads((ROOT / "modules/L5-services/pyproject.toml").read_text())
    assert "uv==0.13.5" in package["project"]["optional-dependencies"]["dev"]
    assert not any(dependency.startswith("uv==") for dependency in package["project"]["dependencies"])
