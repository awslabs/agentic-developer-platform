"""The member_org_ids reconciliation script actually rebuilds what it claims to.

Issue #4849 (#4839 · T0b), PR #4916 review F4. The script
(`scripts/backfill_member_org_ids.py`) is load-bearing by its own docstring: the
write-through in ``project_member_org_ids`` is deliberately best-effort, and that
is only safe because this script exists as the repair path. A repair path with no
tests is a repair path that fails during the incident it exists for.

What these tests pin, against a real (SQLite) database through the script's own
query:

* **Multi-org union for one user** — the projection is the full set of orgs the
  account holds memberships in, not the last one written.
* **Union across user rows sharing one GitHub identity** — the DDB key is per
  GitHub account, and ``user_identities`` is unique per (provider,
  provider_user_id, org_id), so one account can map to N ``users.id`` rows. The
  rebuild must aggregate across all PROVEN bindings (review F1's semantics; the
  write-through was fixed to match, and this test is what keeps the two from
  disagreeing again).
* **Targeted repair filters** — ``--provider-user-id`` selects one account;
  ``--user-id`` selects *which account* to rebuild but must still see that
  account's full membership set (semi-join, not a row filter).
* **A write that cannot land reports failure** — the per-table writers return
  False (row absent) or raise (real DDB fault); ``main`` maps failures to a
  non-zero exit, which is what an operator's runbook keys on.

The script reads no session fixtures — it opens its own engine from a URL — so
these tests build a throwaway file-backed SQLite database with only the three
tables the query touches, created from the real ORM metadata.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization
from src.shared.models.vault import UserIdentity

pytestmark = pytest.mark.asyncio

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_member_org_ids.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_member_org_ids", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


@pytest.fixture
async def db_url(tmp_path):
    """A file-backed SQLite DB with the tables the script's query touches."""
    url = f"sqlite+aiosqlite:///{tmp_path}/backfill.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    Organization.__table__,
                    UserIdentity.__table__,
                    TenantMembership.__table__,
                ],
                checkfirst=True,
            )
        )
    yield url
    await engine.dispose()


async def _seed(db_url: str, rows: list[dict]) -> None:
    """Insert user_identities / tenant_memberships rows via raw SQL.

    Raw SQL, not the ORM: the script's own query is raw SQL against these
    tables, and going through the ORM here would couple the test to model
    defaults the script never sees.
    """
    from sqlalchemy import text

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        for r in rows:
            if r["table"] == "identity":
                await conn.execute(
                    text(
                        "INSERT INTO user_identities "
                        "(id, user_id, provider, provider_user_id, org_id, team_id, verification_method, created_at, updated_at) "
                        "VALUES (:id, :user_id, 'github', :pid, :org_id, :team_id, :method, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": r["id"],
                        "user_id": r["user_id"],
                        "pid": r["pid"],
                        "org_id": r["org_id"],
                        "team_id": f"{r['org_id']}-team-default",
                        "method": r.get("method", "oauth"),
                    },
                )
            else:
                await conn.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, user_id, tenant_id, role, is_active, joined_via, created_at, updated_at) "
                        "VALUES (:id, :user_id, :tenant_id, 'member', 0, 'admin_create', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"id": r["id"], "user_id": r["user_id"], "tenant_id": r["tenant_id"]},
                )
    await engine.dispose()


class TestRebuildQuery:
    async def test_multi_org_user_gets_the_full_union(self, script, db_url):
        await _seed(
            db_url,
            [
                {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"},
                {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
                {"table": "membership", "id": "m2", "user_id": "u1", "tenant_id": "org-b"},
            ],
        )

        users = await script.get_users_with_memberships(db_url)

        assert users == [{"provider_user_id": "123", "provider": "github", "member_org_ids": ["org-a", "org-b"]}]

    async def test_union_spans_user_rows_sharing_one_github_identity(self, script, db_url):
        """The F1 shape: one GitHub account, two users.id rows, different orgs.

        The rebuild is keyed on the ACCOUNT, so it must union org-a and org-b —
        emitting either alone is the clobber the write-through fix closed.
        """
        await _seed(
            db_url,
            [
                {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"},
                {"table": "identity", "id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b"},
                {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
                {"table": "membership", "id": "m2", "user_id": "u2", "tenant_id": "org-b"},
            ],
        )

        users = await script.get_users_with_memberships(db_url)

        assert users == [{"provider_user_id": "123", "provider": "github", "member_org_ids": ["org-a", "org-b"]}]

    async def test_provider_user_id_filter_selects_one_account(self, script, db_url):
        await _seed(
            db_url,
            [
                {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"},
                {"table": "identity", "id": "i2", "user_id": "u2", "pid": "456", "org_id": "org-b"},
                {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
                {"table": "membership", "id": "m2", "user_id": "u2", "tenant_id": "org-b"},
            ],
        )

        users = await script.get_users_with_memberships(db_url, provider_user_id="456")

        assert users == [{"provider_user_id": "456", "provider": "github", "member_org_ids": ["org-b"]}]

    async def test_user_id_filter_still_sees_the_accounts_full_set(self, script, db_url):
        """--user-id picks WHICH account to rebuild; the rebuild is still total.

        Filtering the joined rows by user_id instead of the semi-join would
        rebuild the shared account's key with only u2's orgs — the same partial
        write the sweep exists to repair.
        """
        await _seed(
            db_url,
            [
                {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"},
                {"table": "identity", "id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b"},
                {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
                {"table": "membership", "id": "m2", "user_id": "u2", "tenant_id": "org-b"},
            ],
        )

        users = await script.get_users_with_memberships(db_url, user_id="u2")

        assert users == [{"provider_user_id": "123", "provider": "github", "member_org_ids": ["org-a", "org-b"]}]

    async def test_membershipless_identity_is_emitted_for_clearing(self, script, db_url):
        """Reconciliation must repair a failed revocation write-through too."""
        await _seed(
            db_url,
            [{"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"}],
        )

        assert await script.get_users_with_memberships(db_url) == [{"provider_user_id": "123", "provider": "github", "member_org_ids": []}]

    @pytest.mark.parametrize("method", ["channel_placement", "self_asserted", "magic_link", "oauth", "admin_manual", "admin_attested"])
    async def test_unproven_target_keeps_proven_siblings_without_adding_its_membership(self, script, db_url, method):
        await _seed(
            db_url,
            [
                {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"},
                {"table": "identity", "id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b", "method": method},
                {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
                {"table": "membership", "id": "m2", "user_id": "u2", "tenant_id": "org-b"},
            ],
        )

        expected = ["org-a", "org-b"] if method in {"oauth", "admin_attested"} else ["org-a"]
        # An unproven row still selects the account to repair, not its authority.
        assert await script.get_users_with_memberships(db_url, user_id="u2") == [
            {"provider_user_id": "123", "provider": "github", "member_org_ids": expected}
        ]

    async def test_explicit_account_target_survives_full_identity_removal(self, script, db_url):
        assert await script.get_users_with_memberships(db_url, provider_user_id="123") == [
            {"provider_user_id": "123", "provider": "github", "member_org_ids": []}
        ]


async def test_reconciliation_writes_only_proven_memberships_then_clears_after_last_proof_removal(script, db_url, monkeypatch):
    await _seed(
        db_url,
        [
            {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"},
            {"table": "identity", "id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b", "method": "channel_placement"},
            {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
            {"table": "membership", "id": "m2", "user_id": "u2", "tenant_id": "org-b"},
        ],
    )
    monkeypatch.setattr(script, "DATABASE_URL", db_url)
    monkeypatch.setattr(script, "IDENTITY_INDEX_TABLE", "membership-repair-old")
    monkeypatch.setattr(script, "USER_IDENTITY_INDEX_TABLE", "membership-repair-new")
    monkeypatch.setattr("sys.argv", ["backfill_member_org_ids.py"])

    with mock_aws():
        client = boto3.client("dynamodb", region_name=script.AWS_REGION)
        tables = [
            (script.IDENTITY_INDEX_TABLE, "identity_type", "identity_value", "github_user"),
            (script.USER_IDENTITY_INDEX_TABLE, "provider", "provider_user_id", "github"),
        ]
        for name, pk, sk, provider in tables:
            client.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
            client.put_item(
                TableName=name,
                Item={
                    pk: {"S": provider},
                    sk: {"S": "123"},
                    "user_id": {"S": "u1"},
                    "verification_method": {"S": "oauth"},
                    "member_org_ids": {"L": [{"S": "org-a"}, {"S": "org-b"}]},
                },
            )

        def assert_projection(expected):
            for name, pk, sk, provider in tables:
                item = client.get_item(TableName=name, Key={pk: {"S": provider}, sk: {"S": "123"}})["Item"]
                assert item["member_org_ids"] == {"L": [{"S": org_id} for org_id in expected]}
                assert item["user_id"] == {"S": "u1"}
                assert item["verification_method"] == {"S": "oauth"}

        # Actual script main and both real writers remove the unproven sibling's
        # org while retaining the proven positive and the core identity fields.
        await script.main()
        assert_projection(["org-a"])

        engine = create_async_engine(db_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM user_identities WHERE id = 'i1'"))
                await conn.execute(text("DELETE FROM tenant_memberships WHERE id = 'm1'"))
            await script.main()
            assert_projection([])

            # A fully deleted account can still be explicitly targeted rather
            # than leaving a stale DDB list unreachable by reconciliation.
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM user_identities WHERE id = 'i2'"))
                await conn.execute(text("DELETE FROM tenant_memberships WHERE id = 'm2'"))
            for name, pk, sk, provider in tables:
                client.update_item(
                    TableName=name,
                    Key={pk: {"S": provider}, sk: {"S": "123"}},
                    UpdateExpression="SET member_org_ids = :orgs",
                    ExpressionAttributeValues={":orgs": {"L": [{"S": "org-a"}]}},
                )
            monkeypatch.setattr("sys.argv", ["backfill_member_org_ids.py", "--provider-user-id", "123"])
            await script.main()
            assert_projection([])
        finally:
            await engine.dispose()


class TestWriteFailureIsReported:
    def _client_error(self, code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")

    def test_missing_row_returns_false_not_success(self, script):
        client = MagicMock()
        client.update_item.side_effect = self._client_error("ConditionalCheckFailedException")

        assert script.update_new_table(client, "123", ["org-a"], dry_run=False) is False
        assert script.update_old_table(client, "123", ["org-a"], dry_run=False) is False

    @pytest.mark.parametrize("code", ["ValidationException", "ProvisionedThroughputExceededException", "AccessDeniedException"])
    def test_a_real_ddb_fault_raises_rather_than_reporting_success(self, script, code):
        client = MagicMock()
        client.update_item.side_effect = self._client_error(code)

        with pytest.raises(ClientError):
            script.update_new_table(client, "123", ["org-a"], dry_run=False)
        with pytest.raises(ClientError):
            script.update_old_table(client, "123", ["org-a"], dry_run=False)

    def test_dry_run_writes_nothing(self, script):
        client = MagicMock()

        assert script.update_new_table(client, "123", ["org-a"], dry_run=True) is True
        client.update_item.assert_not_called()


@pytest.mark.parametrize("failed_copies", [{"old"}, {"new"}, {"old", "new"}])
async def test_main_failed_clear_is_nonzero_and_partial_state_is_safely_retryable(script, db_url, monkeypatch, caplog, failed_copies):
    await _seed(
        db_url,
        [
            {"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "channel_placement"},
            {"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"},
        ],
    )
    monkeypatch.setattr(script, "DATABASE_URL", db_url)
    monkeypatch.setattr(script, "IDENTITY_INDEX_TABLE", "failed-clear-old")
    monkeypatch.setattr(script, "USER_IDENTITY_INDEX_TABLE", "failed-clear-new")
    monkeypatch.setattr("sys.argv", ["backfill_member_org_ids.py"])
    caplog.set_level(logging.INFO, logger=script.logger.name)
    tables = [
        ("old", script.IDENTITY_INDEX_TABLE, "identity_type", "identity_value", "github_user"),
        ("new", script.USER_IDENTITY_INDEX_TABLE, "provider", "provider_user_id", "github"),
    ]

    with mock_aws():
        client = boto3.client("dynamodb", region_name=script.AWS_REGION)
        read_keys = {}

        def create_copy(copy, name, pk, sk, provider, *, malformed):
            key = {pk: {"S": provider}, sk: {"S": "123"}}
            if malformed:
                # A real malformed-schema rejection from Moto. The stale row
                # exists; ValidationException does not mean it was absent.
                client.create_table(
                    TableName=name,
                    BillingMode="PAY_PER_REQUEST",
                    KeySchema=[{"AttributeName": "unexpected_key", "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": "unexpected_key", "AttributeType": "S"}],
                )
                read_keys[copy] = {"unexpected_key": {"S": "existing-row"}}
                key.update(read_keys[copy])
            else:
                client.create_table(
                    TableName=name,
                    BillingMode="PAY_PER_REQUEST",
                    KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                    AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
                )
                read_keys[copy] = key.copy()
            client.put_item(
                TableName=name,
                Item=key | {"user_id": {"S": "u1"}, "verification_method": {"S": "oauth"}, "member_org_ids": {"L": [{"S": "revoked-tenant"}]}},
            )

        for copy, name, pk, sk, provider in tables:
            create_copy(copy, name, pk, sk, provider, malformed=copy in failed_copies)

        with pytest.raises(SystemExit) as failure:
            await script.main()
        assert failure.value.code == 1
        partial = int(len(failed_copies) == 1)
        assert f"0 succeeded, 1 failed ({partial} partial), 0 absent table rows, 1 total" in caplog.text
        assert "outcome=already_absent" not in caplog.text
        for copy, name, *_ in tables:
            row = client.get_item(TableName=name, Key=read_keys[copy])["Item"]
            expected_orgs = [{"S": "revoked-tenant"}] if copy in failed_copies else []
            assert row["member_org_ids"] == {"L": expected_orgs}
            assert row["user_id"] == {"S": "u1"}
            assert row["verification_method"] == {"S": "oauth"}

        # Correct only the failed fixture schemas; a second run must retain
        # successful clears and complete the failed copies without bare rows.
        for copy, name, pk, sk, provider in tables:
            if copy in failed_copies:
                client.delete_table(TableName=name)
                create_copy(copy, name, pk, sk, provider, malformed=False)
        caplog.clear()
        await script.main()
        assert "1 succeeded, 0 failed (0 partial), 0 absent table rows, 1 total" in caplog.text
        for copy, name, *_ in tables:
            row = client.get_item(TableName=name, Key=read_keys[copy])["Item"]
            assert row["member_org_ids"] == {"L": []}
            assert row["user_id"] == {"S": "u1"}
            assert row["verification_method"] == {"S": "oauth"}


@pytest.mark.parametrize("has_membership", [False, True])
async def test_main_absent_rows_are_distinct_from_failed_writes_and_never_created(script, db_url, monkeypatch, caplog, has_membership):
    rows = [{"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"}]
    if has_membership:
        rows.append({"table": "membership", "id": "m1", "user_id": "u1", "tenant_id": "org-a"})
    await _seed(db_url, rows)
    monkeypatch.setattr(script, "DATABASE_URL", db_url)
    monkeypatch.setattr(script, "IDENTITY_INDEX_TABLE", "absent-clear-old")
    monkeypatch.setattr(script, "USER_IDENTITY_INDEX_TABLE", "absent-clear-new")
    monkeypatch.setattr("sys.argv", ["backfill_member_org_ids.py"])
    caplog.set_level(logging.INFO, logger=script.logger.name)

    with mock_aws():
        client = boto3.client("dynamodb", region_name=script.AWS_REGION)
        for name, pk, sk in [
            (script.IDENTITY_INDEX_TABLE, "identity_type", "identity_value"),
            (script.USER_IDENTITY_INDEX_TABLE, "provider", "provider_user_id"),
        ]:
            client.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        if has_membership:
            with pytest.raises(SystemExit) as failure:
                await script.main()
            assert failure.value.code == 1
            assert "outcome=missing" in caplog.text
            assert "0 succeeded, 1 failed (0 partial), 2 absent table rows" in caplog.text
        else:
            await script.main()
            assert "outcome=already_absent" in caplog.text
            assert "1 succeeded, 0 failed (0 partial), 2 absent table rows" in caplog.text
        for name in (script.IDENTITY_INDEX_TABLE, script.USER_IDENTITY_INDEX_TABLE):
            assert client.scan(TableName=name)["Items"] == [], "membership repair must not create a bare identity row"
