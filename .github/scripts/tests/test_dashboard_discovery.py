"""Exercise SSM fallback and fail-closed errors with the actual shell script."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "resolve-dashboard-url.sh"


@pytest.mark.parametrize("mode,success,expected", [
    ("url", True, "https://dashboard.example"),
    ("missing", True, "https://distribution.example"),
    ("denied", False, "AccessDeniedException"),
    ("network", False, "Could not connect"),
    ("none", True, "https://distribution.example"),
])
def test_ssm_discovery(tmp_path, mode, success, expected):
    aws = tmp_path / "aws"
    aws.write_text('''#!/usr/bin/env bash
if [[ "$*" == *frontend-url* ]]; then
  case "$MODE" in
    url) echo https://dashboard.example ;;
    none) echo None ;;
    missing) echo ParameterNotFound >&2; exit 254 ;;
    denied) echo AccessDeniedException >&2; exit 254 ;;
    network) echo 'Could not connect' >&2; exit 255 ;;
  esac
else
  echo distribution.example
fi
''')
    aws.chmod(0o755)
    output = tmp_path / "env"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", MODE=mode,
               ENVIRONMENT="dev", GITHUB_ENV=str(output), INPUT_CLOUDFRONT_URL="")
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert (result.returncode == 0) == success
    if success:
        assert output.read_text() == f"E2E_CLOUDFRONT_URL={expected}\n"
    else:
        assert expected in result.stderr
        assert not output.exists()
