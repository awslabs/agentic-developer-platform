"""An explicitly local TLS exception must not permit a remote database target."""

import pytest

from app import schema_boundary as transport


@pytest.fixture(autouse=True)
def local_exception(monkeypatch):
    monkeypatch.delenv(transport.CA_VARIABLE, raising=False)
    monkeypatch.setenv(transport.LOCAL_EXCEPTION_VARIABLE, "true")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://localhost/test",
        "postgresql+asyncpg://127.0.0.1/test",
        "postgresql+asyncpg://[::1]/test",
        "postgresql+asyncpg:///test?host=/tmp/postgres",
        "postgresql+asyncpg:///test?host=localhost:5432&host=127.0.0.1:5433",
    ],
)
def test_explicit_local_targets_remain_supported(url):
    assert transport.connect_args("adp_test", url) == {
        "server_settings": {"search_path": "adp_test"},
        "ssl": "disable",
    }


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://database.example/test",
        "postgresql+asyncpg://10.0.0.8/test",
        "postgresql+asyncpg://[2001:db8::1]/test",
        "postgresql+asyncpg://localhost/test?host=database.example",
        "postgresql+asyncpg:///test?host=localhost:5432&host=database.example:5432",
        "postgresql+asyncpg:///test",
        "postgresql+asyncpg://localhost.example/test",
        "postgresql+asyncpg://postgres.default.svc.cluster.local/test",
        "postgresql+asyncpg://superplane-integration-test-postgres.other.svc.cluster.local/test",
        "postgresql+asyncpg://superplane-integration-test-postgres.superplane-integration-test.svc.cluster.local.example/test",
        "not-a-url",
    ],
)
def test_remote_overridden_mixed_and_implicit_targets_are_refused(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("PGHOST", "database.example")
    with pytest.raises(
        transport.DatabaseTransportUnverifiable, match="explicit loopback"
    ):
        transport.connect_args("adp_test", url)


def test_denial_does_not_expose_database_credentials():
    url = "postgresql+asyncpg://offline:do-not-disclose@remote.example/test"
    with pytest.raises(transport.DatabaseTransportUnverifiable) as caught:
        transport.connect_args("", url)
    assert "do-not-disclose" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_guarded_local_cluster_fixture_uses_a_supported_database_target():
    """Exercise the shipped fixture's actual DSN against the runtime policy."""
    import re
    from pathlib import Path

    import yaml

    deploy = Path(__file__).resolve().parents[1] / "deploy"
    docs = list(yaml.safe_load_all((deploy / "integration-test.yaml").read_text()))
    init = next(
        doc
        for doc in docs
        if doc.get("kind") == "Job"
        and doc["metadata"]["name"] == "superplane-integration-test-secret-init"
    )
    script = init["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    url = re.search(r'--from-literal=DATABASE_URL="([^"]+)"', script).group(1)
    assert transport.connect_args("", url) == {"ssl": "disable"}

    # A local fixture cannot conceal an additional remote failover host.
    url += f"?host={transport.LOCAL_FIXTURE_HOST}:5432&host=remote.example:5432"
    with pytest.raises(transport.DatabaseTransportUnverifiable):
        transport.connect_args("", url)
