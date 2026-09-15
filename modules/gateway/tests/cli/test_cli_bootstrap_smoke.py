"""Release smoke checks fail closed before sending credentials to a wrong build."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/test-cli-bootstrap.py"
spec = importlib.util.spec_from_file_location("cli_bootstrap_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize("correct_binding", [False, True])
def test_binding_and_artifact_failures_never_send_credentials(tmp_path, monkeypatch, capsys, correct_binding):
    calls = []
    expected = tmp_path / "expected"
    expected.mkdir()
    (expected / "install.sh").write_text("candidate installer")

    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("cognito-config"):
            return io.BytesIO(
                json.dumps({"user_pool_id": "pool" if correct_binding else "another", "client_id": "browser", "cli_client_id": "client"}).encode()
            )
        return io.BytesIO(b"previous installer")

    def forbidden(*args, **kwargs):
        pytest.fail("must not read credentials or start a CLI on an unverified build")

    monkeypatch.setattr(smoke.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fetch))
    monkeypatch.setattr(smoke.common, "read_private_json", forbidden)
    monkeypatch.setattr(smoke.subprocess, "run", forbidden)
    result = smoke.main(
        [
            "--gateway-url",
            "https://adp.example/api",
            "--expected-pool",
            "pool",
            "--expected-client",
            "client",
            "--expected-cli-dir",
            str(expected),
            "--credentials-file",
            str(tmp_path / "secret.json"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == ("artifact_mismatch" if correct_binding else "deployment_mismatch")
    assert len(calls) == (2 if correct_binding else 1)
    assert (tmp_path / "report.json").stat().st_mode & 0o777 == 0o600
