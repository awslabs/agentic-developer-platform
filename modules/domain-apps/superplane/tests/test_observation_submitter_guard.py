"""An incomplete authentication result must stop before persistence lookup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("optimization", [[], ["-O"], ["-OO"]])
def test_missing_authenticated_submitter_refused_before_storage(tmp_path, optimization):
    program = """
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from app.services import observations
from superplane_contracts import AuthResult

class ForbiddenDatabase:
    async def get(self, *args, **kwargs):
        raise RuntimeError("storage accessed before submitter validation")

async def check():
    resolver = SimpleNamespace(signing_key_for=lambda credential: b"fixture")
    observation = SimpleNamespace(subject=SimpleNamespace(
        cluster_id="00000000-0000-4000-8000-000000000001"))
    result = AuthResult(authenticated=True, submitter=None, observation=observation)
    with patch.object(observations, "load_submitters", return_value=resolver), patch.object(
        observations, "verify_submission", return_value=result
    ):
        try:
            await observations.record_observation(
                ForbiddenDatabase(), body=b"{}", headers={})
        except observations.ObservationRefused as error:
            if error.status_code != 401 or error.reason != "unauthenticated":
                raise RuntimeError("unexpected refusal") from error
        else:
            raise RuntimeError("incomplete authentication was accepted")
asyncio.run(check())
"""
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=os.pathsep.join(
            [str(DOMAIN / "src/superplane-api"), str(DOMAIN / "contracts")]
        ),
        DATABASE_URL="postgresql+asyncpg://localhost/superplane_offline_test",
        SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS="true",
    )
    result = subprocess.run(
        [sys.executable, *optimization, "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
