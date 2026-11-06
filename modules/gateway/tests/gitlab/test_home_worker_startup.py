"""The dedicated worker must boot without the gateway's authentication secrets."""

import os
import subprocess
import sys
from pathlib import Path


def test_worker_import_does_not_initialize_gateway_authentication():
    environment = dict(os.environ)
    environment.pop("BG_TOKEN_SECRET_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import src.gitlab.home_worker; assert 'src.auth.middleware' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
