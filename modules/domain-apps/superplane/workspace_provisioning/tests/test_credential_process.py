from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError
import time

import pytest

from workspace_provisioning.credential_bridge import credential_environment
from workspace_provisioning.credential_process import read_credentials
from workspace_provisioning.runtime_config import LifecycleRefused


def session(frozen=None):
    return SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(
            _expiry_time=datetime.now(UTC) + timedelta(minutes=10),
            get_frozen_credentials=frozen
            or (
                lambda: SimpleNamespace(
                    access_key="fixture-key",
                    secret_key="fixture-secret",
                    token="fixture-token",
                )
            ),
        )
    )


def test_slow_live_checks_work_for_concurrent_clients(tmp_path):
    checks = []

    def verify():
        time.sleep(2.1)  # Deliberately exceeds the SDK metadata client's timeout.
        checks.append(True)

    with credential_environment(session(), verify, directory=tmp_path) as env:
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: read_credentials(env), range(2)))
    assert len(checks) == 4
    assert all(v["Version"] == 1 and v["AccessKeyId"] == "fixture-key" for v in values)
    assert all(
        v["SessionToken"] == "fixture-token" and "Token" not in v for v in values
    )
    assert list(tmp_path.iterdir()) == []


def test_revocation_during_credential_read_never_delivers(tmp_path):
    revoked = []

    def frozen():
        revoked.append(True)
        return SimpleNamespace(
            access_key="fixture-key", secret_key="fixture-secret", token="fixture-token"
        )

    def verify():
        if revoked:
            raise LifecycleRefused("revoked")

    with credential_environment(session(frozen), verify, directory=tmp_path) as env:
        with pytest.raises(HTTPError) as error:
            read_credentials(env)
        assert error.value.code == 403


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/credentials",
        "https://127.0.0.1:123/credentials",
        "http://127.0.0.1:123/elsewhere",
        "http://user@127.0.0.1:123/credentials",
        "http://127.0.0.1:123/credentials?token=x",
        "http://127.0.0.1/credentials",
    ],
)
def test_process_refuses_nonlocal_or_changed_endpoint(url):
    with pytest.raises(ValueError):
        read_credentials(
            {
                "SUPERPLANE_CREDENTIAL_BRIDGE_URL": url,
                "SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN": "x" * 48,
            }
        )


def test_helper_failure_does_not_print_private_configuration(monkeypatch, capsys):
    from workspace_provisioning.credential_process import main

    monkeypatch.setenv(
        "SUPERPLANE_CREDENTIAL_BRIDGE_URL", "https://example.com/private"
    )
    monkeypatch.setenv("SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN", "private-value" * 8)
    assert main() == 1
    out = capsys.readouterr()
    assert not out.out
    assert out.err == "Operation credential authority unavailable\n"
