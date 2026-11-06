"""The scheduled engine must load recovery without web-login configuration."""

import os
import subprocess
import sys
from pathlib import Path


def test_cancelled_release_recovery_loads_without_web_login_secret(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {key: value for key, value in os.environ.items() if key.upper() != "BG_TOKEN_SECRET_KEY"}
    env.update(
        PYTHONPATH=str(root),
        AWS_EC2_METADATA_DISABLED="true",
        AWS_DEFAULT_REGION="us-east-1",
        AWS_ACCESS_KEY_ID="fixture-access-key",
        AWS_SECRET_ACCESS_KEY="fixture-secret-key",
        OTEL_SDK_DISABLED="true",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import socket
import sys

def refuse_network(*args, **kwargs):
    raise AssertionError("Recovery import must not perform network requests")

socket.socket.connect = refuse_network
from src.orchestration.operations_cancelled_release import reconcile_cancelled_release
from src.orchestration.operations_release_job import NativeReleaseJob
from src.shared.config import get_settings

assert not get_settings().token_secret_key
assert callable(reconcile_cancelled_release)
assert callable(NativeReleaseJob(None).verify_terminal_run)
assert "src.auth.middleware" not in sys.modules
assert "src.agentauth.arc_model" not in sys.modules
""",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
