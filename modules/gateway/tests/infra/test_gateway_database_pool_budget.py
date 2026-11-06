"""Check the default-off HTTP pool budget against the deployment inputs."""

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sqlalchemy.pool import NullPool

from src.shared import database
from src.shared.config import Settings

GATEWAY = Path(__file__).resolve().parents[2]


def _http_environment():
    deployment = yaml.safe_load((GATEWAY / "k8s/deployment.yaml").read_text())
    container = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "bedrockgateway")
    values = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    return deployment, values


def test_default_off_manifest_and_fleet_budget_have_independent_expected_limits():
    deployment, values = _http_environment()
    hpa = yaml.safe_load((GATEWAY / "k8s/hpa.yaml").read_text())
    assert '"--workers", "1"' in (GATEWAY / "Dockerfile").read_text()
    assert hpa["spec"]["maxReplicas"] == 12
    assert deployment["spec"]["strategy"]["rollingUpdate"]["maxSurge"] == "50%"
    assert {
        key: values[key]
        for key in (
            "BG_RDS_POOL_ENABLED",
            "BG_RDS_POOL_SIZE",
            "BG_RDS_POOL_MAX_OVERFLOW",
            "BG_RDS_SPLIT_POOL_ENABLED",
            "BG_RDS_REQUEST_POOL_SIZE",
            "BG_RDS_ACCOUNTING_POOL_SIZE",
            "BG_RDS_CONNECT_TIMEOUT_SECONDS",
            "BG_RDS_LOCK_TIMEOUT_MS",
            "BG_RDS_STATEMENT_TIMEOUT_MS",
        )
    } == {
        "BG_RDS_POOL_ENABLED": "true",
        "BG_RDS_POOL_SIZE": "8",
        "BG_RDS_POOL_MAX_OVERFLOW": "4",
        "BG_RDS_SPLIT_POOL_ENABLED": "false",
        "BG_RDS_REQUEST_POOL_SIZE": "6",
        "BG_RDS_ACCOUNTING_POOL_SIZE": "4",
        "BG_RDS_CONNECT_TIMEOUT_SECONDS": "5",
        "BG_RDS_LOCK_TIMEOUT_MS": "1000",
        "BG_RDS_STATEMENT_TIMEOUT_MS": "8000",
    }
    shared_config = (GATEWAY / "k8s/configmap.yaml").read_text()
    for key in ("BG_RDS_POOL_ENABLED", "BG_RDS_SPLIT_POOL_ENABLED", "BG_RDS_REQUEST_POOL_SIZE", "BG_RDS_ACCOUNTING_POOL_SIZE"):
        assert key not in shared_config
    assert Settings(_env_file=None).rds_pool_enabled is False
    assert Settings(_env_file=None).rds_pool_timeout_seconds == 10

    workers = 1
    replicas = hpa["spec"]["maxReplicas"]
    # A percentage maxSurge is rounded up against spec.replicas at the HPA ceiling.
    surge = math.ceil(replicas * int(deployment["spec"]["strategy"]["rollingUpdate"]["maxSurge"].rstrip("%")) / 100)
    assert surge == 6
    combined = int(values["BG_RDS_POOL_SIZE"]) + int(values["BG_RDS_POOL_MAX_OVERFLOW"])
    split = int(values["BG_RDS_REQUEST_POOL_SIZE"]) + int(values["BG_RDS_ACCOUNTING_POOL_SIZE"])
    assert (combined, split) == (12, 10)
    assert (combined * workers, split * workers) == (12, 10)
    assert (combined * workers * replicas, split * workers * replicas) == (144, 120)
    assert (combined * workers * (replicas + surge), split * workers * (replicas + surge)) == (216, 180)
    # Worst plausible rollout overlap (docs/performance/gateway-database-pooling.md):
    # every old pod still draining while the new fleet plus its surge is up.
    assert (combined * workers * (replicas + surge + 12), split * workers * (replicas + surge + 12)) == (360, 300)
    # Old four-worker pods also permit 12 combined connections each. Count those
    # draining pods explicitly while changing the worker layout in one rollout.
    assert combined * workers * (replicas + surge) + (2 + 1) * 4 * 12 == 360
    # bedrockgw-dev-postgres is db.t3.medium (4 GiB): max_connections =
    # LEAST(DBInstanceClassMemory/9531392, 5000) ~= 450. The combined pool's worst
    # overlap must leave room for the tick, migrations, operators and reserved slots.
    assert combined * workers * (replicas + surge + 12) <= 450 - 60


@pytest.mark.asyncio
async def test_selected_split_rolls_back_to_same_combined_http_pool_and_preserves_lambda_nullpool(monkeypatch):
    _, values = _http_environment()
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unit:unit@db.example.invalid/fixture",
        rds_iam_auth=True,
        rds_host="db.example.invalid",
        rds_pool_enabled=True,
        rds_split_pool_enabled=True,
        rds_pool_size=int(values["BG_RDS_POOL_SIZE"]),
        rds_pool_max_overflow=int(values["BG_RDS_POOL_MAX_OVERFLOW"]),
        rds_request_pool_size=int(values["BG_RDS_REQUEST_POOL_SIZE"]),
        rds_accounting_pool_size=int(values["BG_RDS_ACCOUNTING_POOL_SIZE"]),
    )
    monkeypatch.setattr(database, "get_database_url", lambda: settings.database_url)
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    with patch.object(database.ssl, "create_default_context", return_value=MagicMock()):
        for selected, expected in ((True, (6, 4)), (False, (8, 8)), (False, (None, None))):
            settings.rds_split_pool_enabled = selected
            settings.rds_pool_enabled = expected[0] is not None
            database.reset_engine()
            request_engine = database.get_engine()
            accounting_engine = database.get_accounting_engine()
            try:
                if expected[0] is None:
                    assert request_engine is accounting_engine
                    assert isinstance(request_engine.pool, NullPool)
                elif selected:
                    assert request_engine is not accounting_engine
                    assert (request_engine.pool.size(), accounting_engine.pool.size()) == expected
                    assert request_engine.pool._max_overflow == accounting_engine.pool._max_overflow == 0
                else:
                    assert request_engine is accounting_engine
                    assert database.get_accounting_session_factory() is database.get_session_factory()
                    assert request_engine.pool.size() == 8
                    assert request_engine.pool._max_overflow == 4
            finally:
                await request_engine.dispose()
                if accounting_engine is not request_engine:
                    await accounting_engine.dispose()
                database.reset_engine()
