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
  rebuild must aggregate across ALL of them (review F1's semantics; the
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
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
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
                        "VALUES (:id, :user_id, 'github', :pid, :org_id, :team_id, 'oauth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": r["id"],
                        "user_id": r["user_id"],
                        "pid": r["pid"],
                        "org_id": r["org_id"],
                        "team_id": f"{r['org_id']}-team-default",
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

    async def test_membershipless_user_is_not_emitted(self, script, db_url):
        """Clearing a fully-revoked user is the write-through's job, not the sweep's

        — documented in the script header; this pins that the sweep does not
        invent empty-list writes (which would mask the F5 one-directionality the
        review accepted as documented).
        """
        await _seed(
            db_url,
            [{"table": "identity", "id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a"}],
        )

        assert await script.get_users_with_memberships(db_url) == []


class TestWriteFailureIsReported:
    def _client_error(self, code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")

    def test_missing_row_returns_false_not_success(self, script):
        client = MagicMock()
        client.update_item.side_effect = self._client_error("ValidationException")

        assert script.update_new_table(client, "123", ["org-a"], dry_run=False) is False
        assert script.update_old_table(client, "123", ["org-a"], dry_run=False) is False

    def test_a_real_ddb_fault_raises_rather_than_reporting_success(self, script):
        client = MagicMock()
        client.update_item.side_effect = self._client_error("ProvisionedThroughputExceededException")

        with pytest.raises(ClientError):
            script.update_new_table(client, "123", ["org-a"], dry_run=False)

    def test_dry_run_writes_nothing(self, script):
        client = MagicMock()

        assert script.update_new_table(client, "123", ["org-a"], dry_run=True) is True
        client.update_item.assert_not_called()
