# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Decision history survives rollout and cannot be edited or discarded."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_organization_grant_migration import (
    installation_postgres_url as installation_postgres_url,
    isolated_database as isolated_database,
    migrate,
    pytestmark,  # noqa: F401
)


async def test_bootstrap_runtime_terms_upgrade_preserves_constraints_and_history(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "056_recovery_settlement_claims")
    assert result.returncode == 0, result.stderr

    async def insert(key, scope, terms):
        async with engine.begin() as db:
            await db.execute(
                text("""INSERT INTO operation_reauthorizations
                (reauthorization_id,idempotency_key,original_approval_id,operation_id,
                 org_id,workspace_id,requester,approved_by,plan_digest,expected_version,
                 expires_at,created_at,scope,amendment_json)
                VALUES (:key,:key,'original','operation','org','workspace','human',
                        'approver','digest',3,now()+interval '3 hours',now(),:scope,:terms)
            """),
                {"key": key, "scope": scope, "terms": terms},
            )

    await insert("retained", "unstarted-planning", '{"retained":true}')
    async with engine.connect() as db:
        original = dict(
            (
                await db.execute(
                    text(
                        "SELECT * FROM operation_reauthorizations WHERE reauthorization_id='retained'"
                    )
                )
            )
            .mappings()
            .one()
        )
    amendment = '{"kind":"bootstrap-runtime-extension-v1","max_runtime_seconds":10800}'
    with pytest.raises(IntegrityError, match="planning_budget_terms"):
        await insert("bootstrap", "unstarted-bootstrap", amendment)
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    await insert("bootstrap", "unstarted-bootstrap", amendment)
    await insert(
        "settled", "unstarted-bootstrap", '{"kind":"settled-bootstrap-review-v1"}'
    )
    await insert("without-amendment", "unstarted-bootstrap", None)
    for scope, terms in (
        ("undispatched", amendment),
        ("uncredentialed-planning", amendment),
        ("planning-budget", None),
        ("partial-apply-repair", None),
    ):
        with pytest.raises(IntegrityError, match="planning_budget_terms"):
            await insert("forbidden-" + scope, scope, terms)
    result = migrate(url, schema, "downgrade", "056_recovery_settlement_claims")
    assert result.returncode != 0
    assert "cannot discard bootstrap runtime review history" in result.stderr
    async with engine.connect() as db:
        assert (
            dict(
                (
                    await db.execute(
                        text(
                            "SELECT * FROM operation_reauthorizations WHERE reauthorization_id='retained'"
                        )
                    )
                )
                .mappings()
                .one()
            )
            == original
        )
        assert (
            await db.execute(text("SELECT count(*) FROM operation_reauthorizations"))
        ).scalar_one() == 4
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"


async def test_reauthorization_history_is_immutable(isolated_database):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as db:
        await db.execute(
            text("""INSERT INTO operation_reauthorizations
            (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,workspace_id,
             requester,approved_by,plan_digest,expected_version,expires_at,created_at)
            VALUES ('renewal','request','original','operation','org','workspace','human','approver',
                    'digest',2,now()+interval '1 hour',now())""")
        )
        await db.execute(
            text(
                "UPDATE operation_reauthorizations SET result='allowed-once',decided_at=now()"
            )
        )
    for mutation in [
        "expires_at=now()+interval '2 hours'",
        "result='rejected'",
        "requester='foreign'",
        "original_approval_id='other'",
        "scope='uncredentialed-planning'",
    ]:
        with pytest.raises(Exception, match="immutable"):
            async with engine.begin() as db:
                await db.execute(
                    text("UPDATE operation_reauthorizations SET " + mutation)
                )
    with pytest.raises(Exception, match="immutable"):
        async with engine.begin() as db:
            await db.execute(text("DELETE FROM operation_reauthorizations"))
    async with engine.begin() as db:
        await db.execute(text("UPDATE operation_reauthorizations SET revoked=true"))
    with pytest.raises(Exception, match="immutable"):
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE operation_reauthorizations SET revoked=false")
            )
    result = migrate(url, schema, "downgrade", "045_approval_policy")
    assert result.returncode != 0
    async with engine.connect() as db:
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"


async def test_provider_scope_preserves_history_and_refuses_downgrade(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "046_dispatch_reauthorization")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as db:
        await db.execute(
            text("""INSERT INTO operation_reauthorizations
            (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,
             workspace_id,requester,approved_by,plan_digest,expected_version,expires_at,created_at)
            VALUES ('old','old-request','original','operation','org','workspace','human',
                    'approver','digest',2,now()-interval '1 hour',now()-interval '2 hours')""")
        )
        before = dict(
            (await db.execute(text("SELECT * FROM operation_reauthorizations")))
            .mappings()
            .one()
        )
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    await engine.dispose()  # External DDL invalidates cached SELECT * result types.
    async with engine.begin() as db:
        after = dict(
            (await db.execute(text("SELECT * FROM operation_reauthorizations")))
            .mappings()
            .one()
        )
        assert after.pop("scope") == "undispatched"
        assert after.pop("amendment_json") is None
        assert after == before
        await db.execute(
            text("""INSERT INTO operation_reauthorizations
            (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,
             workspace_id,requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope)
            VALUES ('fresh','fresh-request','original','operation','org','workspace','human',
                    'approver','digest',2,now()+interval '1 hour',now(),'uncredentialed-planning')""")
        )
    result = migrate(url, schema, "downgrade", "046_dispatch_reauthorization")
    assert result.returncode != 0 and "cannot discard" in result.stderr
    async with engine.connect() as db:
        assert (
            await db.execute(text("SELECT count(*) FROM operation_reauthorizations"))
        ).scalar_one() == 2
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"


async def test_daily_budget_amendment_terms_and_decision_are_immutable(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    insert = """INSERT INTO operation_reauthorizations
      (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,workspace_id,
       requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope,amendment_json)
      VALUES (:id,:id,'original','operation','org','workspace','human','approver','digest',7,
        now()+interval '1 hour',now(),'planning-budget',
        :terms)"""
    async with engine.begin() as db:
        await db.execute(
            text(insert),
            {
                "id": "budget",
                "terms": '{"version":1,"previous_daily_micros":0,"daily_micros":1000000000,"previous_workspace_daily_micros":0}',
            },
        )
        await db.execute(
            text(
                "UPDATE operation_reauthorizations SET result='allowed-once',decided_at=now() WHERE reauthorization_id='budget'"
            )
        )
        await db.execute(
            text(insert),
            {
                "id": "other",
                "terms": '{"version":1,"previous_daily_micros":0,"daily_micros":1000000000,"previous_workspace_daily_micros":0}',
            },
        )
    for update in [
        "amendment_json='{}'",
        "scope='unstarted-planning'",
        "plan_digest='other'",
        "expires_at=now()+interval '2 hours'",
        "result='rejected'",
    ]:
        with pytest.raises(Exception, match="immutable"):
            async with engine.begin() as db:
                await db.execute(
                    text(
                        "UPDATE operation_reauthorizations SET "
                        + update
                        + " WHERE reauthorization_id='budget'"
                    )
                )
    with pytest.raises(Exception, match="one_approved_planning_budget"):
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE operation_reauthorizations SET result='allowed-once',decided_at=now() WHERE reauthorization_id='other'"
                )
            )
    result = migrate(url, schema, "downgrade", "048_planning_recovery")
    assert (
        result.returncode != 0
        and "cannot discard reviewed budget history" in result.stderr
    )
    async with engine.connect() as db:
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"


async def test_recovery_confirmation_terms_survive_and_prevent_downgrade(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    insert = """INSERT INTO operation_reauthorizations
      (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,workspace_id,
       requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope,amendment_json)
      VALUES (:id,:id,'original','operation','org','workspace','human','approver','digest',8,
        now()+interval '1 hour',now(),:scope,:terms)"""
    terms = '{"version":1,"budget_reauthorization_id":"original-budget","daily_micros":1000000000}'
    async with engine.begin() as db:
        await db.execute(
            text(insert),
            {"id": "confirmation", "scope": "unstarted-planning", "terms": terms},
        )
        await db.execute(
            text(
                "UPDATE operation_reauthorizations SET result='allowed-once',decided_at=now() WHERE reauthorization_id='confirmation'"
            )
        )
    for scope, value in (
        ("undispatched", terms),
        ("uncredentialed-planning", terms),
        ("planning-budget", None),
    ):
        with pytest.raises(Exception, match="planning_budget_terms"):
            async with engine.begin() as db:
                await db.execute(
                    text(insert), {"id": "invalid", "scope": scope, "terms": value}
                )
    for update in (
        "amendment_json='{}'",
        "expires_at=now()+interval '2 hours'",
        "expected_version=9",
    ):
        with pytest.raises(Exception, match="immutable"):
            async with engine.begin() as db:
                await db.execute(
                    text(
                        "UPDATE operation_reauthorizations SET "
                        + update
                        + " WHERE reauthorization_id='confirmation'"
                    )
                )
    result = migrate(url, schema, "downgrade", "049_planning_budget")
    assert (
        result.returncode != 0
        and "cannot discard reviewed budget confirmation history" in result.stderr
    )
    async with engine.connect() as db:
        assert (
            await db.execute(
                text("SELECT amendment_json FROM operation_reauthorizations")
            )
        ).scalar_one() == terms
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"


async def test_saved_completion_scope_and_budget_confirmation_cannot_be_downgraded(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as db:
        await db.execute(
            text("""INSERT INTO operation_reauthorizations
        (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,workspace_id,
         requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope,amendment_json)
        VALUES ('completion','completion-review','original','operation','org','workspace',
                'human','approver','digest',10,now()+interval '1 hour',now(),'saved-plan-completion','{}')""")
        )
    result = migrate(url, schema, "downgrade", "050_planning_budget_confirmation")
    assert (
        result.returncode != 0
        and "cannot discard saved plan completion authority" in result.stderr
    )
    async with engine.connect() as db:
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"
        assert (
            await db.execute(text("SELECT scope FROM operation_reauthorizations"))
        ).scalar_one() == "saved-plan-completion"


async def test_partial_repair_registry_and_decision_history_are_retained(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as db:
        await db.execute(
            text("""INSERT INTO workspace_partial_apply_preparations
        (operation_id,org_id,workspace_id,source_operation_id,source_fence,source_version,
         plan_digest,original_allocation_id,owner_evidence_sha256,owner_evidence_json)
        VALUES ('preparation','org','workspace','original',1,2,
                repeat('a',64),'original-allocation',repeat('b',64),'{}')""")
        )
    for statement in (
        "UPDATE workspace_partial_apply_preparations SET source_fence=2",
        "UPDATE workspace_partial_apply_preparations SET owner_evidence_json='[]'",
        "DELETE FROM workspace_partial_apply_preparations",
    ):
        with pytest.raises(Exception, match="immutable"):
            async with engine.begin() as db:
                await db.execute(text(statement))
    result = migrate(url, schema, "downgrade", "051_saved_plan_completion")
    assert result.returncode != 0 and "cannot discard partial apply" in result.stderr
    insert = """INSERT INTO operation_reauthorizations
    (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,workspace_id,
     requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope,amendment_json)
    VALUES ('repair','review','original','operation','org','workspace','human','approver',
            'digest',2,now()+interval '1 hour',now(),'partial-apply-repair',:terms)"""
    with pytest.raises(Exception, match="planning_budget_terms"):
        async with engine.begin() as db:
            await db.execute(text(insert), {"terms": None})
    async with engine.begin() as db:
        await db.execute(
            text(insert), {"terms": '{"repair":{"artifact_id":"exact-plan"}}'}
        )
        await db.execute(
            text(
                "UPDATE operation_reauthorizations SET result='allowed-once',decided_at=now()"
            )
        )
    with pytest.raises(Exception, match="immutable"):
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE operation_reauthorizations SET amendment_json='{}'")
            )
    async with engine.connect() as db:
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"
        assert (
            await db.execute(
                text("SELECT count(*) FROM workspace_partial_apply_preparations")
            )
        ).scalar_one() == 1


async def test_fresh_preparation_migration_preserves_immutable_prior_rows(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "052_partial_apply_repair")
    assert result.returncode == 0, result.stderr
    statement = text("""INSERT INTO workspace_partial_apply_preparations
        (operation_id,org_id,workspace_id,source_operation_id,source_fence,source_version,
         plan_digest,original_allocation_id,owner_evidence_sha256,owner_evidence_json)
        VALUES (:operation,'org','workspace','original-apply',1,2,:digest,'allocation',:evidence,'{}')""")
    first = {"operation": "first", "digest": "a" * 64, "evidence": "b" * 64}
    async with engine.begin() as db:
        await db.execute(statement, first)
        original = dict(
            (
                await db.execute(
                    text("SELECT * FROM workspace_partial_apply_preparations")
                )
            )
            .mappings()
            .one()
        )
    with pytest.raises(Exception, match="uq_partial_apply_preparation"):
        async with engine.begin() as db:
            await db.execute(
                statement,
                {"operation": "fresh", "digest": "c" * 64, "evidence": "d" * 64},
            )
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    await engine.dispose()
    async with engine.begin() as db:
        assert (
            dict(
                (
                    await db.execute(
                        text("SELECT * FROM workspace_partial_apply_preparations")
                    )
                )
                .mappings()
                .one()
            )
            == original
        )
        await db.execute(
            statement, {"operation": "fresh", "digest": "c" * 64, "evidence": "d" * 64}
        )
    for mutation in [
        "UPDATE workspace_partial_apply_preparations SET owner_evidence_json='changed'",
        "DELETE FROM workspace_partial_apply_preparations",
    ]:
        with pytest.raises(Exception, match="immutable"):
            async with engine.begin() as db:
                await db.execute(text(mutation))
    result = migrate(url, schema, "downgrade", "052_partial_apply_repair")
    assert (
        result.returncode != 0
        and "cannot discard fresh preparation history" in result.stderr
    )
    async with engine.connect() as db:
        assert (
            await db.execute(
                text("SELECT count(*) FROM workspace_partial_apply_preparations")
            )
        ).scalar_one() == 2


async def test_bootstrap_scope_upgrade_preserves_history_and_blocks_downgrade(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    assert (
        migrate(url, schema, "upgrade", "053_partial_preparation_refresh").returncode
        == 0
    )
    insert = text("""INSERT INTO operation_reauthorizations
        (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,
         workspace_id,requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope)
        VALUES (:id,:id,'original','operation','org','workspace','human','approver',
                'digest',2,now()+interval '1 hour',now(),:scope)""")
    async with engine.begin() as db:
        await db.execute(insert, {"id": "retained", "scope": "unstarted-planning"})
        before = dict(
            (await db.execute(text("SELECT * FROM operation_reauthorizations")))
            .mappings()
            .one()
        )
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as db:
        assert (
            dict(
                (await db.execute(text("SELECT * FROM operation_reauthorizations")))
                .mappings()
                .one()
            )
            == before
        )
    result = migrate(url, schema, "downgrade", "053_partial_preparation_refresh")
    assert result.returncode == 0, result.stderr
    assert migrate(url, schema, "upgrade", "head").returncode == 0
    async with engine.begin() as db:
        await db.execute(insert, {"id": "bootstrap", "scope": "unstarted-bootstrap"})
    result = migrate(url, schema, "downgrade", "053_partial_preparation_refresh")
    assert result.returncode != 0 and "cannot discard bootstrap" in result.stderr
    async with engine.connect() as db:
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"
        assert (
            await db.execute(text("SELECT count(*) FROM operation_reauthorizations"))
        ).scalar_one() == 2


async def test_interrupted_plan_scope_retains_decisions_and_prevents_downgrade(isolated_database):
    _, engine, url, _, schema, _ = isolated_database
    assert migrate(url, schema, "upgrade", "head").returncode == 0
    async with engine.begin() as db:
        await db.execute(text("""INSERT INTO operation_reauthorizations
            (reauthorization_id,idempotency_key,original_approval_id,operation_id,org_id,workspace_id,
             requester,approved_by,plan_digest,expected_version,expires_at,created_at,scope)
            VALUES ('interrupted','review','original','operation','org','workspace','human','approver',
                    'unchanged-plan',2,now()+interval '1 hour',now(),'interrupted-planning')"""))
    result = migrate(url, schema, "downgrade", "057_bootstrap_recovery_terms")
    assert result.returncode != 0 and "cannot discard interrupted planning decisions" in result.stderr
    async with engine.connect() as db:
        assert (await db.execute(text("SELECT scope FROM operation_reauthorizations"))).scalar_one() == "interrupted-planning"
        assert (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "058_interrupted_planning"
