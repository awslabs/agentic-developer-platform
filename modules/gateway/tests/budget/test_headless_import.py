"""Exercise the tick's imports outside pytest's configured web-auth process."""

import os
import subprocess
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[2]


def run_fresh(code, *, web_key=None):
    env = os.environ.copy()
    env.pop("BG_TOKEN_SECRET_KEY", None)
    env.pop("JWT_SECRET_KEY", None)
    env.update(PYTHONPATH=str(GATEWAY), BG_REDIS_URL="", AWS_EC2_METADATA_DISABLED="true")
    if web_key is not None:
        env["BG_TOKEN_SECRET_KEY"] = web_key
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=GATEWAY, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_tick_reads_budget_without_web_signing_key():
    run_fresh("""
import asyncio
import sys
from src.orchestration.flow_meter import read_flow_meter
from src.orchestration.flow_budget import get_flow_reservations

# With no configured Redis, the real meter remains unavailable, never zero.
assert asyncio.run(read_flow_meter(org_id="test-org", flow_id="test-flow", policy=None)) is None
assert not get_flow_reservations().enabled
assert "src.budget.routes" not in sys.modules
assert "src.auth.middleware" not in sys.modules
""")


def test_explicit_web_router_keeps_its_signing_key_requirement():
    run_fresh("""
try:
    from src.budget import budget_router
except ValueError as error:
    assert "BG_TOKEN_SECRET_KEY" in str(error)
else:
    raise AssertionError("web authentication must still require its signing key")
""")


def test_public_web_router_export_remains_compatible():
    run_fresh("""
from src.budget import budget_router, BudgetService
from src.budget.routes import router
from src.budget.service import BudgetService as Service
assert budget_router is router
assert BudgetService is Service
assert router.routes
""", web_key="headless-import-test-signing-key")
