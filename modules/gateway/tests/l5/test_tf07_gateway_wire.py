"""Exercise the scoped gateway clients and persisted scheduled replay offline."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.transform_client_probe import probe_clients
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.internal import tools_l5
from tests.l5.tf07_gateway_driver import GatewayClientDriver

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example", "relationship_revision": "1"}
RUN_ID = "run-example"
RESULT_ID = "result-example"
EVENT_ID = "schedule:2026-01-01T00:00:00Z:revision:1"


@pytest.fixture
def transform_contract(monkeypatch):
    root = Path(__file__).resolve().parents[4] / "docs/l5-services"
    monkeypatch.setattr(tools_l5, "files", lambda package: root)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    yield
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()


class Backend:
    transform_result_reader_ready = True

    def __init__(self):
        self.run = SimpleNamespace(
            run_id=RUN_ID,
            service_id="transform",
            revision=2,
            state="outcome_unknown",
            provider_state="unknown",
            scope=SCOPE,
            admission={"scope": SCOPE},
            deadline_at=NOW,
            origin="adp_submitted",
            input={},
            native_job_ref=None,
            provider_readback_ref=None,
            monitoring=None,
            retry_of=None,
        )
        self.result = {
            "service_id": "transform",
            "result_id": RESULT_ID,
            "run_id": RUN_ID,
            "revision": 3,
            "scope": SCOPE,
            "verification_status": "unknown",
            "cost_status": "unknown",
        }
        self.calls = []

    async def get_run(self, product_id, project_id, service_id, run_id):
        self.calls.append((product_id, project_id, service_id, "run"))
        if (product_id, project_id, service_id, run_id) != (SCOPE["product_id"], SCOPE["project_id"], "transform", RUN_ID):
            raise HTTPException(404, detail={"code": "not_found"})
        return self.run

    async def result_store(self, product_id, project_id, service_id):
        self.calls.append((product_id, project_id, service_id, "result"))
        if (product_id, project_id, service_id) != (SCOPE["product_id"], SCOPE["project_id"], "transform"):
            raise HTTPException(404, detail={"code": "not_found"})
        return SimpleNamespace(transform_reader_ready=True, get=self.get_result)

    async def get_result(self, result_id):
        if result_id != RESULT_ID:
            raise HTTPException(404, detail={"code": "not_found"})
        return self.result


@pytest.fixture
async def driver(tmp_path, transform_contract):
    cli_path = Path(__file__).resolve().parents[2] / "cli/adp-service.py"
    spec = importlib.util.spec_from_file_location("transform_cli_tf07", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    async with engine.begin() as database:
        await database.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all(
            [
                Configuration(
                    tenant_id=SCOPE["tenant_id"],
                    configuration_id="config-example",
                    product_id=SCOPE["product_id"],
                    project_id=SCOPE["project_id"],
                    service_id="transform",
                    relationship_revision="1",
                    owner_id="owner-example",
                    current_revision=1,
                    handoff_state="linked",
                ),
                ConfigurationVersion(
                    tenant_id=SCOPE["tenant_id"],
                    configuration_id="config-example",
                    revision=1,
                    settings={"enabled": True, "triggers": []},
                    inherited_fields=[],
                    gaps=[],
                    state="ready",
                    hash="a" * 64,
                ),
                EventReceipt(
                    tenant_id=SCOPE["tenant_id"],
                    product_id=SCOPE["product_id"],
                    project_id=SCOPE["project_id"],
                    service_id="transform",
                    trigger_id="trigger-example",
                    event_id=EVENT_ID,
                    idempotency_key="tick-example",
                    event_kind="schedule",
                    configuration_id="config-example",
                    configuration_revision=1,
                    target={},
                    run_input={},
                    state="admitted",
                    reason=None,
                    run_id="scheduled-example",
                    received_at=NOW,
                ),
                Run(
                    tenant_id=SCOPE["tenant_id"],
                    run_id="scheduled-example",
                    product_id=SCOPE["product_id"],
                    project_id=SCOPE["project_id"],
                    service_id="transform",
                    configuration_id="config-example",
                    configuration_revision=1,
                    binding_id="binding-example",
                    binding_generation=1,
                    trigger_id="trigger-example",
                    deadline_at=NOW,
                    input={},
                    admission={"scope": SCOPE, "event_id": EVENT_ID},
                    scope=SCOPE,
                ),
            ]
        )
        await session.commit()

    async def scm_reader():
        return {
            "before": {"revision": "base-example", "pull_requests": 0},
            "after": {"revision": "base-example", "pull_requests": 0},
            "pr_actions": [],
            "evidence_ref": "offline/scm-readback",
        }

    selected = GatewayClientDriver(backend=Backend(), cli=cli, sessions=sessions, scope=SCOPE, scheduled_at=NOW, scm_reader=scm_reader)
    yield selected
    await engine.dispose()


@pytest.mark.asyncio
async def test_actual_gateway_clients_and_schedule_replay_keep_one_occurrence(driver):
    driver.verify_no_pr_handoff()
    report = await probe_clients(driver, scope=SCOPE, run_id=RUN_ID, result_id=RESULT_ID, trigger_id="trigger-example")
    assert report["live_qualified"] is False and report["occurrence_count"] == 1
    assert set(report["client_refs"]) == {"ui", "cli", "mcp"}
    assert driver.backend.calls == [(SCOPE["product_id"], SCOPE["project_id"], "transform", action) for action in ("run", "result") * 3]


@pytest.mark.asyncio
async def test_gateway_readback_refuses_foreign_scope_and_missing_replay(driver):
    driver.backend.result = {**driver.backend.result, "scope": {**SCOPE, "tenant_id": "foreign"}}
    with pytest.raises(ValueError, match="scoped result"):
        await probe_clients(driver, scope=SCOPE, run_id=RUN_ID, result_id=RESULT_ID, trigger_id="trigger-example")
    driver.backend.result = {**driver.backend.result, "scope": SCOPE}
    async with driver.sessions() as session:
        receipt = await session.get(
            EventReceipt, (SCOPE["tenant_id"], SCOPE["product_id"], SCOPE["project_id"], "transform", "trigger-example", EVENT_ID)
        )
        receipt.state = "rejected"
        await session.commit()
    with pytest.raises(ValueError, match="scheduled replay"):
        await probe_clients(driver, scope=SCOPE, run_id=RUN_ID, result_id=RESULT_ID, trigger_id="trigger-example")


@pytest.mark.asyncio
async def test_gateway_mcp_rejects_foreign_service_before_any_read(driver):
    with pytest.raises(HTTPException) as error:
        tools_l5.l5_http_request(
            "l5___get_run", {"product_id": SCOPE["product_id"], "project_id": SCOPE["project_id"], "service_id": "transform", "assessment_ref": ""}
        )
    assert error.value.status_code == 422
    assert driver.backend.calls == []
