"""Offline binding and retry regressions: real SQLite query and Moto conditions."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.identity.verification import PROVEN_METHODS, UNPROVEN_METHODS, is_proven
from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.models.vault import UserIdentity

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_identity_provenance.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("backfill_identity_provenance", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def db_url(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/provenance.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[Organization.__table__, UserIdentity.__table__]))
    yield url
    await engine.dispose()


async def _seed(db_url, rows):
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        for row in rows:
            await conn.execute(
                text("""INSERT INTO user_identities
                    (id, user_id, provider, provider_user_id, org_id, team_id, verification_method, created_at, updated_at)
                    VALUES (:id, :user_id, :provider, :pid, :org_id, :team_id, :method, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""),
                {**row, "provider": row.get("provider", "github"), "team_id": f"{row['org_id']}-team-default"},
            )
    await engine.dispose()


def _source(*, user="u1", org="org-a", method="oauth", pid="123", identity="i1", provider="github"):
    return {"id": identity, "user_id": user, "pid": pid, "org_id": org, "method": method, "provider": provider}


def _key(table):
    if table == "legacy":
        return {"identity_type": {"S": "github_user"}, "identity_value": {"S": "123"}}
    return {"provider": {"S": "github"}, "provider_user_id": {"S": "123"}}


def _put(client, table, *, user="u1", org="org-a", method=None):
    item = _key(table) | {
        "user_id": {"S": user},
        "org_id": {"S": org},
        "member_org_ids": {"L": [{"S": "org-a"}, {"S": "org-b"}]},
        "user_kind": {"S": "human"},
        "bot_kind": {"S": "none"},
        "updated_at": {"S": "before-backfill"},
    }
    if method is not None:
        item["verification_method"] = {"S": method}
    client.put_item(TableName=table, Item=item)
    return item


def _get(client, table):
    return client.get_item(TableName=table, Key=_key(table)).get("Item")


@pytest.fixture
def ddb(script, monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for table in ("legacy", "v2"):
            columns = list(_key(table))
            client.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": columns[0], "KeyType": "HASH"}, {"AttributeName": columns[1], "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": col, "AttributeType": "S"} for col in columns],
            )
        monkeypatch.setattr(script, "IDENTITY_INDEX_TABLE", "legacy")
        monkeypatch.setattr(script, "USER_IDENTITY_INDEX_TABLE", "v2")
        monkeypatch.setattr(script.boto3, "client", lambda *args, **kwargs: client)
        yield client


@pytest.fixture
def main_args(script, db_url, monkeypatch):
    monkeypatch.setattr(script, "DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["backfill_identity_provenance.py"])


@pytest.mark.parametrize("method", sorted(PROVEN_METHODS | UNPROVEN_METHODS | {""}))
async def test_matching_source_copies_each_method_without_upgrading(script, db_url, ddb, method):
    await _seed(db_url, [_source(method=method)])
    account = (await script.get_identity_provenance(db_url))[0]
    assert account == {
        "provider": "github",
        "provider_user_id": "123",
        "bindings": [{"user_id": "u1", "org_id": "org-a", "verification_method": method}],
    }
    for table, writer in (("legacy", script.update_old_table), ("v2", script.update_new_table)):
        original = _put(ddb, table)
        assert writer(ddb, account, dry_run=False) == "updated"
        result = _get(ddb, table)
        assert result["verification_method"] == {"S": method}
        assert is_proven(result["verification_method"]["S"]) == is_proven(method)
        for key in original.keys() - {"updated_at"}:
            assert result[key] == original[key]


@pytest.mark.parametrize("method_b", ["oauth", "self_asserted", "admin_manual"])
async def test_multitenant_proof_follows_each_exact_source_binding(script, db_url, ddb, method_b):
    await _seed(db_url, [_source(), _source(user="u2", org="org-b", method=method_b, identity="i2")])
    account = (await script.get_identity_provenance(db_url, user_id="u1"))[0]
    assert len(account["bindings"]) == 2  # --user-id selects the full account.
    _put(ddb, "legacy", user="u1", org="org-a")
    _put(ddb, "v2", user="u2", org="org-b")
    assert script.update_old_table(ddb, account, False) == "updated"
    assert script.update_new_table(ddb, account, False) == "updated"
    assert _get(ddb, "legacy")["verification_method"] == {"S": "oauth"}
    assert _get(ddb, "v2")["verification_method"] == {"S": method_b}
    # Membership/routing attributes are untouched; any_adp_user is a reader policy.
    assert _get(ddb, "v2")["member_org_ids"] == {"L": [{"S": "org-a"}, {"S": "org-b"}]}


async def test_query_filters_accounts_and_keeps_provider_namespace(script, db_url):
    await _seed(db_url, [_source(), _source(pid="456", identity="i2"), _source(provider="slack", identity="i3")])
    assert [a["provider_user_id"] for a in await script.get_identity_provenance(db_url)] == ["123", "456"]
    result = await script.get_identity_provenance(db_url, provider_user_id="456")
    assert len(result) == 1
    assert result[0]["provider_user_id"] == "456"
    assert result[0]["provider"] == "github"
    assert await script.get_identity_provenance(db_url, user_id="missing") == []


async def test_legacy_never_projects_another_provider(script, db_url, ddb):
    await _seed(db_url, [_source()])
    account = (await script.get_identity_provenance(db_url))[0] | {"provider": "slack"}
    original = _put(ddb, "legacy")
    with pytest.raises(ValueError, match="only supports GitHub"):
        script.update_old_table(ddb, account, False)
    assert _get(ddb, "legacy") == original
    assert script.update_new_table(ddb, account, False) == "missing"


@pytest.mark.parametrize("table", ["legacy", "v2"])
@pytest.mark.parametrize("user,org", [("stale-user", "org-a"), ("u1", "stale-org"), ("u2", "org-a")])
@pytest.mark.parametrize("existing_method", [None, "oauth"])
async def test_stale_or_mixed_binding_clears_proof_and_reports_incomplete(script, db_url, ddb, table, user, org, existing_method):
    await _seed(db_url, [_source(), _source(user="u2", org="org-b", identity="i2")])
    account = (await script.get_identity_provenance(db_url))[0]
    original = _put(ddb, table, user=user, org=org, method=existing_method)
    writer = script.update_old_table if table == "legacy" else script.update_new_table
    assert writer(ddb, account, False) == "mismatched"
    item = _get(ddb, table)
    assert not is_proven(item["verification_method"]["S"])
    for key in original.keys() - {"verification_method", "updated_at"}:
        assert item[key] == original[key]


@pytest.mark.parametrize("missing", ["user_id", "org_id"])
async def test_incomplete_projected_binding_is_denied(script, db_url, ddb, missing):
    await _seed(db_url, [_source()])
    account = (await script.get_identity_provenance(db_url))[0]
    item = _put(ddb, "v2", method="oauth")
    del item[missing]
    ddb.put_item(TableName="v2", Item=item)
    assert script.update_new_table(ddb, account, False) == "mismatched"
    assert not is_proven(_get(ddb, "v2")["verification_method"]["S"])


async def test_ambiguous_exact_binding_does_not_select_proof(script, db_url, ddb):
    await _seed(db_url, [_source()])
    account = (await script.get_identity_provenance(db_url))[0]
    # Current DB uniqueness prevents this, but conflicting input is still not proof.
    account["bindings"].append(account["bindings"][0] | {"verification_method": "self_asserted"})
    _put(ddb, "v2", method="oauth")
    assert script.update_new_table(ddb, account, False) == "ambiguous"
    assert not is_proven(_get(ddb, "v2")["verification_method"]["S"])


@pytest.mark.parametrize("changed", ["user_id", "org_id", "verification_method", "updated_at", "deleted"])
async def test_concurrent_projection_replacement_is_not_overwritten(script, db_url, ddb, changed):
    await _seed(db_url, [_source()])
    account = (await script.get_identity_provenance(db_url))[0]
    original = _put(ddb, "v2")
    replacement = original | {changed: {"S": "concurrent-change"}}
    real_update = ddb.update_item

    def race(**kwargs):
        if changed == "deleted":
            ddb.delete_item(TableName="v2", Key=_key("v2"))
        else:
            ddb.put_item(TableName="v2", Item=replacement)
        return real_update(**kwargs)

    with patch.object(ddb, "update_item", side_effect=race):
        assert script.update_new_table(ddb, account, False) == "conflict"
    assert _get(ddb, "v2") == (None if changed == "deleted" else replacement)


async def test_missing_required_projection_is_not_upserted_or_counted_complete(script, db_url, ddb, main_args, caplog):
    await _seed(db_url, [_source()])
    _put(ddb, "legacy")
    # Unrelated legacy installation/reverse keys are not provenance targets.
    unrelated = {"identity_type": {"S": "github_installation_id"}, "identity_value": {"S": "999"}, "org_id": {"S": "org-a"}}
    ddb.put_item(TableName="legacy", Item=unrelated)
    with caplog.at_level("INFO"), pytest.raises(SystemExit) as result:
        await script.main()
    assert result.value.code == 1
    assert _get(ddb, "v2") is None
    assert "0 complete, 1 incomplete (1 partial)" in caplog.text
    assert ddb.get_item(TableName="legacy", Key={key: unrelated[key] for key in ("identity_type", "identity_value")})["Item"] == unrelated
    _put(ddb, "v2")  # Canonical mapping repair, followed by a safe retry.
    await script.main()
    assert _get(ddb, "v2")["verification_method"] == {"S": "oauth"}


@pytest.mark.parametrize("failed_table", ["legacy", "v2"])
async def test_dual_write_partial_fault_reports_failure_and_retry_repairs(script, db_url, ddb, main_args, caplog, failed_table):
    await _seed(db_url, [_source()])
    original = {table: _put(ddb, table) for table in ("legacy", "v2")}
    real_update = ddb.update_item

    def fail_one(**kwargs):
        if kwargs["TableName"] == failed_table:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "offline fault"}}, "UpdateItem")
        return real_update(**kwargs)

    with caplog.at_level("INFO"), patch.object(ddb, "update_item", side_effect=fail_one), pytest.raises(SystemExit) as result:
        await script.main()
    assert result.value.code == 1
    assert _get(ddb, failed_table) == original[failed_table]
    other = "legacy" if failed_table == "v2" else "v2"
    assert _get(ddb, other)["verification_method"] == {"S": "oauth"}
    assert "0 complete, 1 incomplete (1 partial)" in caplog.text
    await script.main()
    await script.main()
    for table in ("legacy", "v2"):
        assert _get(ddb, table)["verification_method"] == {"S": "oauth"}
        assert _get(ddb, table)["member_org_ids"] == original[table]["member_org_ids"]


async def test_validation_errors_are_errors_for_both_tables(script, db_url, ddb, main_args, monkeypatch, caplog):
    await _seed(db_url, [_source()])
    ddb.create_table(
        TableName="wrong-schema",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "different", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "different", "AttributeType": "S"}],
    )
    monkeypatch.setattr(script, "IDENTITY_INDEX_TABLE", "wrong-schema")
    monkeypatch.setattr(script, "USER_IDENTITY_INDEX_TABLE", "wrong-schema")
    with caplog.at_level("INFO"), pytest.raises(SystemExit) as result:
        await script.main()
    assert result.value.code == 1
    assert "legacy:error" in caplog.text and "v2:error" in caplog.text
    assert "0 complete, 1 incomplete (0 partial)" in caplog.text
    assert "missing; mapping repair required" not in caplog.text


async def test_validation_error_on_update_is_not_misreported_as_missing(script, db_url, ddb):
    await _seed(db_url, [_source()])
    _put(ddb, "v2")
    account = (await script.get_identity_provenance(db_url))[0]
    fault = ClientError({"Error": {"Code": "ValidationException", "Message": "offline rejected update"}}, "UpdateItem")
    with patch.object(ddb, "update_item", side_effect=fault), pytest.raises(ClientError):
        script.update_new_table(ddb, account, False)


@pytest.mark.parametrize("stale", [False, True])
async def test_dry_run_is_inert_and_reports_mapping_gaps(script, db_url, ddb, main_args, monkeypatch, stale):
    await _seed(db_url, [_source()])
    before = {table: _put(ddb, table, user="stale" if stale else "u1", method="self_asserted") for table in ("legacy", "v2")}
    source_before = await script.get_identity_provenance(db_url)
    monkeypatch.setattr("sys.argv", ["backfill_identity_provenance.py", "--dry-run"])
    with patch.object(ddb, "update_item", side_effect=AssertionError("dry run must not write")) as writes:
        if stale:
            with pytest.raises(SystemExit) as result:
                await script.main()
            assert result.value.code == 1
        else:
            await script.main()
        writes.assert_not_called()
    assert await script.get_identity_provenance(db_url) == source_before
    assert {table: _get(ddb, table) for table in ("legacy", "v2")} == before


@pytest.mark.parametrize("mutation", ["UPDATE user_identities SET user_id='u2', verification_method='self_asserted'", "DELETE FROM user_identities"])
async def test_main_refreshes_source_before_projection(script, db_url, ddb, main_args, monkeypatch, mutation):
    await _seed(db_url, [_source()])
    for table in ("legacy", "v2"):
        _put(ddb, table, method="oauth")
    real_query = script.get_identity_provenance

    async def replaced_source(*args, **kwargs):
        snapshot = await real_query(*args, **kwargs)
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(text(mutation))
        await engine.dispose()
        return snapshot

    monkeypatch.setattr(script, "get_identity_provenance", replaced_source)
    with pytest.raises(SystemExit):
        await script.main()
    for table in ("legacy", "v2"):
        assert not is_proven(_get(ddb, table)["verification_method"]["S"])


async def test_requested_missing_account_exits_nonzero(script, db_url, ddb, main_args, monkeypatch):
    monkeypatch.setattr("sys.argv", ["backfill_identity_provenance.py", "--provider-user-id", "missing"])
    with pytest.raises(SystemExit) as result:
        await script.main()
    assert result.value.code == 1


@pytest.fixture
def local_postgres():
    # Always a disposable local server: never use an environment-provided DB URL.
    pgserver = pytest.importorskip("pgserver")
    with tempfile.TemporaryDirectory(prefix="adp-backfill-pg-", dir="/tmp") as data:
        server = pgserver.get_server(data)
        try:
            yield server.get_uri()
        finally:
            server.cleanup()


@pytest.mark.parametrize("mutation", ["UPDATE user_identities SET verification_method='self_asserted'", "DELETE FROM user_identities"])
async def test_main_holds_canonical_rows_through_each_projection_attempt(script, ddb, local_postgres, monkeypatch, mutation):
    import psycopg2

    url = local_postgres.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text("""CREATE TABLE user_identities (
            provider text NOT NULL, provider_user_id text NOT NULL, user_id text NOT NULL,
            org_id text NOT NULL, verification_method text NOT NULL,
            UNIQUE(provider, provider_user_id, org_id))""")
        )
        await conn.execute(text("INSERT INTO user_identities VALUES ('github', '123', 'u1', 'org-a', 'oauth')"))
    for table in ("legacy", "v2"):
        _put(ddb, table)
    monkeypatch.setattr(script, "DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_identity_provenance.py"])
    connection = psycopg2.connect(local_postgres)
    connection.autocommit = True
    attempted = []
    real_update = ddb.update_item

    def change_source_during_write(**kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '100ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cursor.execute(mutation)
        attempted.append(kwargs["TableName"])
        return real_update(**kwargs)

    try:
        with patch.object(ddb, "update_item", side_effect=change_source_during_write):
            await script.main()
        assert attempted == ["legacy", "v2"]
        for table in attempted:
            assert _get(ddb, table)["verification_method"] == {"S": "oauth"}
        # The source is writable after the account transaction is released.
        with connection.cursor() as cursor:
            cursor.execute(mutation)
    finally:
        connection.close()
        await engine.dispose()
