"""Full live-command failures must never print raw data, even with --showlocals."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from superplane_acceptance import features

MODULE_ROOT = Path(features.__file__).parents[1]
PRIVATE = "probe-private-response-sentinel"
TOKEN = "probe-private-token-sentinel"

# sitecustomize installs the offline HTTP boundary before the REAL live test runs.
# Every case refuses; no fake success or live evidence record is produced.
HOOK = r"""
import os
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
from superplane_acceptance import features

CASE = os.environ["FEATURE_FAILURE_CASE"]
PRIVATE = "probe-private-response-sentinel"
BODIES = {
    "schema": b'{"features":{},"private":"probe-private-response-sentinel"}',
    "json": b'{"private":"probe-private-response-sentinel"',
    "duplicate": b'{"features":{},"private":"probe-private-response-sentinel","private":"x"}',
    "constant": b'{"features":{},"private":"probe-private-response-sentinel","bad":NaN}',
}
class Response:
    status = 200
    def __init__(self, url):
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Date"] = format_datetime(datetime.now(timezone.utc), usegmt=True)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def geturl(self):
        return self.url
    def read(self, limit):
        return BODIES[CASE][:limit]
class Opener:
    def open(self, request, timeout):
        if CASE == "transport":
            raise urllib.error.URLError(PRIVATE)
        if CASE == "redirect":
            return features.NoRedirect().redirect_request(
                request, None, 302, "Found", {}, "https://foreign.example/?code=" + PRIVATE
            )
        return Response(request.full_url)
features.urllib.request.build_opener = lambda *args: Opener()
"""


@pytest.mark.parametrize(
    "case,reason",
    [
        ("schema", "required feature field"),
        ("json", "Malformed feature JSON"),
        ("duplicate", "Duplicate JSON field"),
        ("constant", "Non-JSON numeric constant"),
        ("redirect", "Feature request redirected"),
        ("transport", "authenticated feature read failed"),
    ],
)
def test_live_entry_failure_output_is_redacted(tmp_path, case, reason):
    hook = tmp_path / "sitecustomize.py"
    hook.write_text(HOOK)
    evidence = tmp_path / "observation.json"
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SUPERPLANE_LIVE_")
        and k not in {"PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
    }
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join([str(tmp_path), str(MODULE_ROOT)]),
            "FEATURE_FAILURE_CASE": case,
            "SUPERPLANE_LIVE_ENVIRONMENT": "embark1/dev",
            "SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE": str(evidence),
            "SUPERPLANE_LIVE_ADP_TOKEN": TOKEN,
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(MODULE_ROOT / "tests/acceptance/test_u1_features_live.py"),
            "-q",
            "--tb=long",
            "--showlocals",
        ],
        cwd=MODULE_ROOT.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed" in output and "EvidenceError" in output, output
    assert reason in output, output
    assert "skipped" not in output
    assert not evidence.exists()
    assert TOKEN not in output
    assert PRIVATE not in output
