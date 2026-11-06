"""Installed shared resources must include the RDS trust roots they consume."""

import ssl
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import NullPool

from l5_services import runtime_support


@pytest.fixture
def database_environment(monkeypatch):
    for name, value in {
        "L5_RDS_HOST": "example.database.invalid",
        "L5_RDS_PORT": "5432",
        "L5_RDS_DATABASE": "example",
        "L5_RDS_USER": "example-reader",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.asyncio
async def test_sessions_use_packaged_trust_and_fresh_iam_tokens(database_environment, monkeypatch):
    engine_factory = runtime_support.create_async_engine
    created, token_requests, connections = {}, [], []

    def create_engine(url, *, async_creator, poolclass):
        assert url == "postgresql+asyncpg://" and poolclass is NullPool
        created["connect"] = async_creator
        return engine_factory("sqlite+aiosqlite://", poolclass=poolclass)

    def generate_token(**request):
        token_requests.append(request)
        return f"offline-token-{len(token_requests)}"

    def client(service, *, region_name):
        assert service == "rds" and region_name == "us-east-1"
        return SimpleNamespace(generate_db_auth_token=generate_token)

    async def connect(**request):
        connections.append(request)
        return object()

    monkeypatch.setattr(runtime_support, "create_async_engine", create_engine)
    monkeypatch.setattr(runtime_support.boto3, "client", client)
    monkeypatch.setattr(runtime_support.asyncpg, "connect", connect)
    sessions = runtime_support.sessions()
    try:
        assert token_requests == [] and connections == []
        await created["connect"]()
        await created["connect"]()
        assert token_requests == [{"DBHostname": "example.database.invalid", "Port": 5432,
                                   "DBUsername": "example-reader", "Region": "us-east-1"}] * 2
        assert [request["password"] for request in connections] == ["offline-token-1", "offline-token-2"]
        for request in connections:
            assert (request["host"], request["port"], request["database"], request["user"]) == (
                "example.database.invalid", 5432, "example", "example-reader")
            context = request["ssl"]
            assert isinstance(context, ssl.SSLContext)
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            assert context.cert_store_stats()["x509_ca"] > 0
    finally:
        await sessions.kw["bind"].dispose()


@pytest.mark.parametrize("port", ["0", "65536"])
def test_sessions_reject_invalid_database_ports(database_environment, monkeypatch, port):
    monkeypatch.setenv("L5_RDS_PORT", port)
    with pytest.raises(ValueError, match="invalid RDS port"):
        runtime_support.sessions()
