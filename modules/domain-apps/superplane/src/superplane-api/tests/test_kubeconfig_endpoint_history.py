"""Actual mounted-HTTP regression evidence at the pre-enforcement revision."""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

HISTORICAL_REVISION = "a84d7a03bc409ecea3d3921ee66afabd25e934f0"
API_PATH = "modules/domain-apps/superplane/src/superplane-api"


@pytest.fixture(scope="module")
def historical_api(tmp_path_factory):
    archive = subprocess.run(
        ["git", "archive", HISTORICAL_REVISION, API_PATH],
        cwd=Path(__file__).resolve().parents[6],
        check=True,
        capture_output=True,
    ).stdout
    checkout = tmp_path_factory.mktemp("historical-kubeconfig")
    with tarfile.open(fileobj=io.BytesIO(archive)) as archive_file:
        archive_file.extractall(checkout, filter="data")
    return checkout / API_PATH


def test_historical_mounted_kubeconfig_allows_same_org_nonmember(historical_api):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_") and key not in {"PYTHONPATH", "DATABASE_URL"}
    }
    env.update(
        PYTHONPATH=str(historical_api),
        AWS_EC2_METADATA_DISABLED="true",
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        AWS_CONFIG_FILE=os.devnull,
    )
    probe = Path(__file__).with_name("historical_kubeconfig_probe.py")
    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=historical_api,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        pytest.fail(result.stderr)
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert [item["status"] for item in evidence["results"]] == [200, 200, 404]
    assert evidence["assume_calls"] == 2
    assert [item["cluster"] for item in evidence["results"][:2]] == [
        "https://cluster.example.invalid"
    ] * 2
    assert all(item["exec"] == "aws" for item in evidence["results"][:2])
