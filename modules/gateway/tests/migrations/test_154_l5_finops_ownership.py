"""Gateway upgrades retain FinOps ownership, scoped claims, and setup history."""

from hashlib import sha256

import pytest
import rfc8785
from l5_services.models import Binding, Configuration, RetainedAgentScope, SetupIntent, SetupResourceReceipt
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_finops_bridge_extends_publication_history():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("154_l5_finops_ownership").down_revision == "153_l5_publications"
    assert scripts.get_revision("153_l5_publications").down_revision == "152_merge_gitlab_prevention"


@pytest.mark.parametrize(
    "second_tenant,second_project,conflicted",
    [
        ("tenant-a", "project-a", False),
        ("tenant-a", "project-b", True),
        ("tenant-b", "project-a", True),
    ],
)
def test_gateway_upgrade_backfills_claims_and_retains_receipts(pg_url, second_tenant, second_project, conflicted):
    upgrade(pg_url, "153_l5_publications")
    engine = create_engine(pg_url)
    identity = {"account_id": "123456789012", "region": "us-east-1"}
    native_ref = {"record_type": "finops_agent", "record_id": "agent-example"}
    try:
        with engine.begin() as connection:
            assert not inspect(connection).has_table(RetainedAgentScope.__tablename__)
            assert not inspect(connection).has_table(SetupResourceReceipt.__tablename__)
            for tenant_id, project_id, identifier, state, ownership in (
                ("tenant-a", "project-a", "first", "ready", "owned"),
                (second_tenant, second_project, "second", "retired", "adopted"),
            ):
                scope = {"tenant_id": tenant_id, "product_id": "product-a", "project_id": project_id}
                configuration_id = "first" if (tenant_id, project_id) == ("tenant-a", "project-a") else "second"
                if identifier == configuration_id:
                    connection.execute(
                        Configuration.__table__.insert().values(
                            **scope,
                            configuration_id=configuration_id,
                            service_id="finops",
                            relationship_revision="1",
                            owner_id="owner",
                            current_revision=1,
                            handoff_state="linked",
                        )
                    )
                connection.execute(
                    Binding.__table__.insert().values(
                        **scope,
                        binding_id=identifier,
                        configuration_id=configuration_id,
                        service_id="finops",
                        scope=scope,
                        revision=1,
                        generation=1,
                        state=state,
                        ownership=ownership,
                        connection_ref={},
                        connection_key=identifier,
                        native_resource_ref=native_ref,
                        native_key="agent-key",
                        provider_identity=identity,
                        readiness=[],
                        gaps=[],
                    )
                )
            connection.execute(
                SetupIntent.__table__.insert().values(
                    tenant_id="tenant-a",
                    operation_id="setup-first",
                    product_id="product-a",
                    project_id="project-a",
                    service_id="finops",
                    actor_id="owner",
                    idempotency_key="setup-key",
                    configuration_id="first",
                    scope={"project_id": "project-a"},
                    request={"action": "create"},
                    authorization={},
                    intent_hash="a" * 64,
                )
            )
            bindings_before = connection.execute(select(Binding).order_by(Binding.binding_id)).mappings().all()
            intent_before = connection.execute(select(SetupIntent)).mappings().one()

        upgrade(pg_url)
        with engine.begin() as connection:
            inspector = inspect(connection)
            for model in (RetainedAgentScope, SetupResourceReceipt):
                assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
                assert set(inspector.get_pk_constraint(model.__tablename__)["constrained_columns"]) == {
                    column.name for column in model.__table__.primary_key
                }
            receipt_columns = inspector.get_columns(SetupResourceReceipt.__tablename__)
            assert isinstance(next(column for column in receipt_columns if column["name"] == "native_resource_ref")["type"], JSONB)
            claim = dict(connection.execute(select(RetainedAgentScope)).mappings().one())
            assert claim["identity_digest"] == sha256(rfc8785.dumps({**identity, "agent_id": "agent-example"})).hexdigest()
            assert claim["conflicted"] is conflicted
            assert claim["service_id"] == "finops"
            assert (claim["tenant_id"], claim["product_id"], claim["project_id"], claim["binding_id"]) in {
                ("tenant-a", "product-a", "project-a", "first"),
                (second_tenant, "product-a", second_project, "second"),
            }
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(RetainedAgentScope.__table__.insert().values({**claim, "tenant_id": "foreign-tenant"}))
            for resource_kind, ownership in (("agent", "owned"), ("view", "adopted")):
                values = dict(
                    tenant_id="tenant-a",
                    operation_id="setup-first",
                    resource_kind=resource_kind,
                    ownership=ownership,
                    state="uncertain",
                    revision=3,
                    native_resource_ref=native_ref,
                    evidence_digest="b" * 64,
                )
                connection.execute(SetupResourceReceipt.__table__.insert().values(**values))
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(SetupResourceReceipt.__table__.insert().values({**values, "tenant_id": "foreign-tenant"}))
            receipts_before = connection.execute(select(SetupResourceReceipt).order_by(SetupResourceReceipt.resource_kind)).mappings().all()

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "153_l5_publications")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert connection.execute(select(Binding).order_by(Binding.binding_id)).mappings().all() == bindings_before
            assert connection.execute(select(SetupIntent)).mappings().one() == intent_before
            assert dict(connection.execute(select(RetainedAgentScope)).mappings().one()) == claim
            assert connection.execute(select(SetupResourceReceipt).order_by(SetupResourceReceipt.resource_kind)).mappings().all() == receipts_before
    finally:
        engine.dispose()
