"""Exercise publication races without Docker or cloud credentials."""

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "modules/agent-factory/agent/scripts/publish-chat-sandbox-image.sh"
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize("push_error,probe_fails,success", [
    ("tag invalid: The image tag already exists and is immutable", False, True),
    ("tag invalid: The image tag already exists and is immutable", True, False),
    ("denied: authorization token expired", False, False),
])
def test_concurrent_publication_requires_verified_winner(tmp_path, push_error, probe_fails, success):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    aws = binaries / "aws"
    aws.write_text('''#!/bin/bash
if [ "$1 $2" = 'ecr get-login-password' ]; then echo synthetic; exit; fi
if [ ! -f "$MOCK_STATE" ]; then
  touch "$MOCK_STATE"
  echo 'ImageNotFoundException' >&2
  exit 1
fi
echo "$MOCK_DIGEST"
''')
    docker = binaries / "docker"
    docker.write_text('''#!/bin/bash
echo "$*" >> "$MOCK_COMMANDS"
case "$1" in
 login) cat >/dev/null ;;
 image) echo sha256:local ;;
 push) echo "$MOCK_PUSH_ERROR" >&2; exit 1 ;;
esac
''')
    for path in (aws, docker):
        path.chmod(0o755)
    probe = tmp_path / "modules/agent-factory/agent/scripts/chat-sandbox-probe-image.test.sh"
    probe.parent.mkdir(parents=True)
    probe.write_text('''echo "probe $1" >> "$MOCK_COMMANDS"
if [[ "$1" == *@* && "$MOCK_PROBE_FAILS" == true ]]; then exit 1; fi
''')
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
           "ADP_SOURCE_SHA": "b" * 40,
           "REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com", "AWS_REGION": "us-east-1",
           "MOCK_STATE": str(tmp_path / "lookup"), "MOCK_DIGEST": DIGEST,
           "MOCK_COMMANDS": str(tmp_path / "commands"), "MOCK_PUSH_ERROR": push_error,
           "MOCK_PROBE_FAILS": str(probe_fails).lower()}
    result = subprocess.run(["bash", str(SCRIPT)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == success, result.stderr
    commands = (tmp_path / "commands").read_text()
    if "already exists" in push_error:
        winner = f"{env['REGISTRY']}/adp-chat-agent@{DIGEST}"
        assert f"pull {winner}" in commands
        assert f"probe {winner}" in commands
    else:
        assert not any(line.startswith("pull ") for line in commands.splitlines())
