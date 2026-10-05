"""Execute the workflow selector against real changed-file Git fixtures."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[4] / ".github/workflows/agent-context-images-build.yml"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("modules/agent-context/personal_context/graph.py", ["context-mcp", "ingestion"]),
        ("modules/agent-context/door/server.py", ["context-mcp"]),
        ("docs/unrelated.md", []),
    ],
)
def test_changed_sources_select_all_packaging_images(tmp_path, path, expected):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init")
    git("commit", "--allow-empty", "-m", "base")
    source = tmp_path / path
    source.parent.mkdir(parents=True)
    source.write_text("fixture")
    git("add", path)
    git("commit", "-m", "change")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = next(
        s["run"] for s in workflow["jobs"]["detect-changes"]["steps"] if s.get("id") == "detect"
    )
    script = script.replace("${{ github.event_name }}", "push").replace("${{ inputs.image }}", "")
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
    )
    assert json.loads(output.read_text().strip().removeprefix("matrix="))["image"] == expected
