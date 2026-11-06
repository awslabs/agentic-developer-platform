"""API recovery must use the domain connection for workspace and artifact reads."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.routers.controller_recovery import Claim, StatusRequest
from app.services import lifecycle_recovery
from workspace_provisioning.lifecycle_policy import policy_document
from workspace_provisioning.tests.postgres_bridge import requires_harness_postgres
from workspace_provisioning.tests.test_applied_recovery_postgres import (
    interrupted_apply,
)
from workspace_provisioning.tests.test_lifecycle_runtime_postgres import (
    harness as harness,
)

pytestmark = requires_harness_postgres


@pytest.mark.parametrize("observed", [False, True])
def test_actual_api_apply_recovery_with_separate_domain_schema(
    harness, tmp_path, monkeypatch, observed  # noqa: F811
):
    async def run():
        scenario, _, applying, artifact, recovery, state = await interrupted_apply(
            harness, tmp_path, monkeypatch, observed=observed
        )
        # A search path cannot silently find another owner's tables. Harness
        # admission, calls and claims remain in the original schema.
        async with harness.connect() as connection:
            await connection.execute("CREATE SCHEMA recovery_domain")
            names = await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() "
                "AND tablename NOT LIKE 'harness_%'"
            )
            for row in names:
                name = row["tablename"]
                assert name.replace("_", "").isalnum()
                await connection.execute(
                    f'ALTER TABLE "{name}" SET SCHEMA recovery_domain'
                )

        @asynccontextmanager
        async def domain_connect():
            async with harness.connect() as connection, connection.transaction():
                await connection.execute("SET LOCAL search_path TO recovery_domain")
                yield connection

        recovery.provider.domain_pool = SimpleNamespace(acquire=domain_connect)
        recovery.context.domain_connect = domain_connect
        document = tmp_path / "api-recovery-policy.json"
        document.write_text(
            json.dumps(
                {
                    "version": 1,
                    "tenants": {
                        applying.grant.lease.org_id: policy_document(scenario.policy)
                    },
                }
            )
        )
        monkeypatch.setattr(settings, "superplane_lifecycle_config_file", str(document))
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    trust_composition=SimpleNamespace(
                        operation_connect=harness.connect, domain_connect=domain_connect
                    )
                )
            )
        )

        @asynccontextmanager
        async def provider(**arguments):
            await arguments["current"]()

            async def read(service, method, **kwargs):
                await arguments["current"]()
                state.reads.append((service, method, kwargs))
                return getattr(
                    scenario.session.client(service, region_name="us-east-1"), method
                )(**kwargs)

            yield SimpleNamespace(aws_read=read, inventory_session=scenario.session)

        monkeypatch.setattr(lifecycle_recovery, "observation_provider", provider)

        async def observe(claim, key, **binding):
            body = StatusRequest(
                claim=Claim(
                    **{name: getattr(claim, name) for name in Claim.model_fields}
                ),
                query_id="real-api-recovery",
                idempotency_key=key,
            )
            result = await lifecycle_recovery.observe_lifecycle(request, body)
            assert result["result_artifact_id"] == binding["artifact_id"]
            return result["facts"]

        recovery.context.authority.lifecycle = observe
        calls_before = tuple(scenario.process_calls)
        (result,) = await recovery.run(limit=1)
        assert result.action == "succeeded" and state.reads
        assert tuple(scenario.process_calls) == calls_before
        async with domain_connect() as connection:
            original = await connection.fetchrow(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                artifact["artifact_id"],
            )
            assert dict(original) == artifact
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT attempts FROM harness_operation_leases WHERE operation_id=$1",
                    applying.grant.lease.operation_id,
                )
                == 1
            )

    harness.run(run())
