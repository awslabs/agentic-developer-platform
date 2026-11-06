from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def test_pr_ci_guards_contract_before_any_release():
    workflow = yaml.load(
        (ROOT / ".github/workflows/tools-interceptor-ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "pull_request" in workflow["on"]
    assert "modules/tools/interceptor/**" in workflow["on"]["pull_request"]["paths"]
    steps = workflow["jobs"]["verify"]["steps"]
    commands = [step.get("run", "") for step in steps]
    assert any("ruff check " in command for command in commands)
    assert any("python -m pytest -q modules/tools/interceptor/tests" in command for command in commands)
    assert all("aws " not in command and "upload-artifact" not in str(step) for command, step in zip(commands, steps))
    assert "id-token" not in workflow["permissions"]
