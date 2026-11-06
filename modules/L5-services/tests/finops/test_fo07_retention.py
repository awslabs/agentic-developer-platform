"""Offline retained-result checks for the FO07 retirement procedure."""

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.findings import ResultStore
from l5_services.finops_reconciliation import normalize_cost_result
from l5_services.models import AssessmentResult, Binding, Configuration, ConfigurationVersion
from l5_services.models import Publication, ResultDetail, Run
from l5_services.models.base import Base
from l5_services.publication.contributions import create_finops_publications
from test_fo04 import cost_case


def test_installed_retention_components_match_release():
    installed_root = os.environ.get("FO07_INSTALLED_ROOT")
    if installed_root is None:
        pytest.skip("only checked in the installed-wheel retention selector")

    from l5_services.api import bindings, router
    from l5_services import findings
    from l5_services.models import setup
    from l5_services.publication import client, contributions, invalidation, recovery

    installed = Path(installed_root).resolve()
    for component in (bindings, router, findings, setup, client, contributions,
                      invalidation, recovery):
        assert Path(component.__file__).resolve().is_relative_to(installed)


@pytest.mark.parametrize(("case", "expected_amount", "comparison"), [
    ("zero", "0.000", "exact"),
    ("missing", None, "unavailable"),
    ("stale", "10.00000001", "incomparable"),
])
async def test_unlink_preserves_result_and_pending_publications(case, expected_amount, comparison, tmp_path):
    fixture, native, billing = cost_case(case)
    now = datetime(2026, 2, 3, tzinfo=UTC)
    scope = fixture.admission["scope"]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retained.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id="config-example",
                                      product_id=scope["product_id"], project_id=scope["project_id"],
                                      service_id="finops", relationship_revision="1", owner_id=scope["owner_id"],
                                      current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="config-example",
                                             revision=1, settings={}, inherited_fields=[], gaps=[],
                                             state="ready", hash="a" * 64))
            binding = Binding(tenant_id=scope["tenant_id"], binding_id="binding-example",
                              configuration_id="config-example", product_id=scope["product_id"],
                              project_id=scope["project_id"], service_id="finops", scope=scope,
                              revision=1, generation=1, state="ready", ownership="adp_created",
                              connection_ref={"record_id": "connection-example"},
                              connection_key="connection-example", native_key="agent-example",
                              readiness=[], gaps=[])
            run = Run(tenant_id=scope["tenant_id"], run_id=fixture.run_id,
                      product_id=scope["product_id"], project_id=scope["project_id"],
                      service_id="finops", configuration_id="config-example",
                      configuration_revision=1, binding_id=binding.binding_id,
                      trigger_id="manual-example", binding_generation=1,
                      state="succeeded" if case == "zero" else "partial",
                      provider_state="completed", input=fixture.input, admission=fixture.admission,
                      native_job_ref=fixture.native_job_ref,
                      provider_readback_ref=fixture.provider_readback_ref,
                      deadline_at=now + timedelta(days=1))
            cost = normalize_cost_result(run, native, billing)
            result = AssessmentResult(tenant_id=scope["tenant_id"], result_id="result-example",
                                      run_id=run.run_id, version=1, payload={})
            detail = ResultDetail(tenant_id=scope["tenant_id"], result_id=result.result_id,
                                  payload={"result_id": result.result_id, "revision": 1,
                                           "scope": scope, **cost})
            publications = create_finops_publications(run, result, detail, now=now)
            session.add_all([binding, run, result, detail, *publications])
            await session.commit()

        async with sessions() as session:
            stored_binding = await session.get(Binding, (scope["tenant_id"], "binding-example"))
            stored_binding.state = "unlinked"
            await session.commit()
        async with sessions() as session:
            stored_run = await session.get(Run, (scope["tenant_id"], fixture.run_id))
            assert stored_run.state == ("succeeded" if case == "zero" else "partial")
            retained = await ResultStore(session, [stored_run]).get("result-example")
            assert retained["oracle"]["measure"]["amount"] == expected_amount
            assert retained["comparison"]["state"] == comparison
            assert retained["native"]["answer_artifact"] == fixture.provider_readback_ref["answer_artifact"]
            assert (await session.get(AssessmentResult, (scope["tenant_id"], "result-example"))).version == 1
            rows = (await session.scalars(select(Publication))).all()
            assert {row.destination for row in rows} == {"product-context", "engineering-insights"}
            assert all(row.state == "pending" and row.receipt is None for row in rows)
            assert all(row.payload["cost"] == cost for row in rows)
            with pytest.raises(HTTPException) as denied:
                await ResultStore(session, []).get("result-example")
            assert denied.value.status_code == 404
    finally:
        await engine.dispose()
