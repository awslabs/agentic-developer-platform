"""Load the real zero-argument WA entry point from the release worker archive."""

import os
import subprocess
import zipfile
from pathlib import Path


MODULE = Path(__file__).resolve().parents[2]


def test_worker_archive_loads_well_architected_with_mounted_resources(tmp_path):
    built = subprocess.run([str(MODULE / "scripts/build-package.sh"), str(tmp_path / "wheel")],
                           check=True, capture_output=True, text=True)
    wheel = Path(built.stdout.strip().splitlines()[-1])
    archive = tmp_path / "workers.zip"
    subprocess.run([str(MODULE / "scripts/build-workers.sh"), str(wheel), str(archive)],
                   check=True, capture_output=True, text=True)
    installed = tmp_path / "installed"
    with zipfile.ZipFile(archive) as package:
        assert "l5_services/runtime_well_architected.py" in package.namelist()
        assert "l5_services/runtime_support.py" in package.namelist()
        assert "l5_services/certs/rds-global-bundle.pem" in package.namelist()
        package.extractall(installed)
    environment = {**os.environ, "PYTHONPATH": "", "AWS_REGION": "us-east-1",
                   "AWS_ACCESS_KEY_ID": "fictional", "AWS_SECRET_ACCESS_KEY": "fictional",
                   "AWS_SESSION_TOKEN": "fictional",
                   "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
                   "AWS_EC2_METADATA_DISABLED": "true", "AWS_LAMBDA_FUNCTION_NAME": "fictional-wa-worker",
                   "L5_SERVICE_ID": "well_architected",
                   "L5_WORKER_FACTORY": "l5_services.runtime_well_architected:factory",
                   "L5_GATEWAY_URL": "https://testapi.execute-api.us-east-1.amazonaws.com/dev",
                   "L5_RDS_HOST": "database.example.invalid", "L5_RDS_PORT": "5432",
                   "L5_RDS_DATABASE": "example", "L5_RDS_USER": "wa_worker",
                   "L5_ARCHIVE_BUCKET": "fictional-wa-bucket",
                   "L5_WORK_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/wa-work"}
    environment.pop("L5_WA_PEER_CONTRACTS", None)
    program = """
import asyncio
import socket
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def no_network(*args, **kwargs):
    raise AssertionError("loading a worker must not contact a provider")
socket.socket.connect = no_network
from l5_services.workers.execution import load_service
from l5_services import runtime_well_architected
assert Path(runtime_well_architected.__file__).is_relative_to(sys.argv[1])
worker = load_service()
assert worker.service_id == "well_architected"
assert worker.well_architected_adapter is not None
assert callable(worker.well_architected_preflight)
assert callable(worker.well_architected_verifier)
assert worker.well_architected_context_client is None
assert worker.well_architected_insights_client is None
asyncio.run(worker.source_client.transport.aclose())
"""
    subprocess.run(["uv", "run", "--no-project", "--python", "3.12", "python", "-I", "-c",
                    program, str(installed)], check=True, capture_output=True, text=True,
                   cwd=tmp_path, env=environment)
