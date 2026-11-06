"""Chat evidence must survive source checkout in push and engine deployments."""
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
PUBLISHER = Path("modules/gateway/scripts/publish-deployment-evidence.py")


@pytest.mark.parametrize("engine", [False, True], ids=["ordinary-push", "engine-dispatch"])
def test_chat_publishers_execute_workflow_revision_after_source_cleanup(tmp_path, engine):
    document = yaml.safe_load((ROOT / ".github/workflows/chat-agent-deploy.yml").read_text())
    steps = document["jobs"]["build-and-deploy"]["steps"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    published = 0
    for step in steps:
        if step.get("uses", "").startswith("actions/checkout@"):
            condition = step.get("if")
            if condition:
                assert condition == "${{ inputs.adp_correlation != '' }}"
                if not engine:
                    continue
            config = step["with"]
            target = tmp_path / config.get("path", ".")
            if target == tmp_path:
                # actions/checkout cleans the candidate workspace, including
                # the earlier nested credential-client checkout on engine runs.
                subprocess.run(["git", "clean", "-ffdx"], cwd=tmp_path, check=True, capture_output=True)
                revision = "candidate-source"
            else:
                assert config["ref"] == "${{ github.workflow_sha }}"
                assert config["persist-credentials"] is False
                revision = "trusted-workflow"
            publisher = target / PUBLISHER
            publisher.parent.mkdir(parents=True, exist_ok=True)
            publisher.write_text(f"print({revision!r})\n")
        elif "publish-deployment-evidence.py" in step.get("run", ""):
            command = shlex.split(step["run"])
            # Execute the workflow's actual script path after checkout cleanup.
            result = subprocess.run(command[:2], cwd=tmp_path, check=True, capture_output=True, text=True)
            assert result.stdout.strip() == "trusted-workflow"
            published += 1
    assert published == 3  # Context plus the two verified release records.
