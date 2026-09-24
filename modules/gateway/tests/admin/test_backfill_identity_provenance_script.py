"""The provenance projection script projects proof and never invents it — #5664 (A10).

`scripts/backfill_identity_provenance.py` is load-bearing in two directions, so a
bug in it is a security bug either way:

* Project too LITTLE and the webhook authority gate (which now denies unproven
  provenance unconditionally) refuses legitimate senders — the outage the previous
  slice's allow-by-default flag existed to avoid.
* Project too MUCH and the gate is decorative: a row that claims `oauth` when
  Postgres holds a self-asserted link mints human dispatch authority from a claim
  nobody verified, which is the A10 finding reopening.

The reduction rule is the interesting part and is tested hardest. One external
account holds N `user_identities` rows (the unique index is per provider +
provider_user_id + org_id) which may carry DIFFERENT provenance, while the DDB key
has no org component and therefore one slot. These tests pin that disagreement
collapses to unknown rather than to the most permissive value.

Same harness as `test_backfill_member_org_ids_script.py`: the script opens its own
engine from a URL, so a throwaway file-backed SQLite database is built from the
real ORM metadata and the script's own raw-SQL query runs against it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.identity.verification import PROVEN_METHODS, UNPROVEN_METHODS, is_proven
from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.models.vault import UserIdentity

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_identity_provenance.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_identity_provenance", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


@pytest.fixture
async def db_url(tmp_path):
    """A file-backed SQLite DB with the one table the script's query touches."""
    url = f"sqlite+aiosqlite:///{tmp_path}/provenance.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[Organization.__table__, UserIdentity.__table__],
                checkfirst=True,
            )
        )
    yield url
    await engine.dispose()


async def _seed(db_url: str, rows: list[dict]) -> None:
    """Insert user_identities rows via raw SQL, matching the script's own query."""
    from sqlalchemy import text

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        for r in rows:
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
                    "method": r["method"],
                },
            )
    await engine.dispose()


class TestReductionRule:
    """The pure function, exhaustively — no database, so the rule stands alone."""

    @pytest.mark.parametrize("method", sorted(PROVEN_METHODS | UNPROVEN_METHODS))
    def test_unanimous_agreement_projects_that_method(self, script, method):
        """Every declared method round-trips when the account's rows agree.

        Parametrized over the policy sets rather than a hard-coded list so a new
        method added to the vocabulary is covered here automatically instead of
        silently escaping the projection.
        """
        assert script.reduce_provenance({method}) == method

    def test_disagreement_projects_unknown(self, script):
        """The load-bearing case: proven in one tenant, unproven in another.

        Projecting "oauth" here would let proof earned in org A mint authority for
        an event routed to org B. The DDB row cannot say which tenant a resolution
        is about, so the only correct answer is "ask Postgres".
        """
        assert script.reduce_provenance({"oauth", "self_asserted"}) == script.UNKNOWN_PROVENANCE

    def test_disagreement_between_two_proven_methods_also_projects_unknown(self, script):
        """Not just proven-vs-unproven: the rule is agreement, not trust level.

        Two rows that are each proof, of different tenants, still do not tell the
        reader which tenant's proof applies. A rule that special-cased
        "all proven → project one of them" would be projecting a fact about org A
        onto a lookup for org B.
        """
        assert script.reduce_provenance({"oauth", "admin_manual"}) == script.UNKNOWN_PROVENANCE

    def test_unknown_mixed_with_proven_projects_unknown(self, script):
        """A NULL/blank row is a disagreement, not something to ignore."""
        assert script.reduce_provenance({"oauth", ""}) == script.UNKNOWN_PROVENANCE

    def test_empty_input_projects_unknown(self, script):
        assert script.reduce_provenance(set()) == script.UNKNOWN_PROVENANCE

    def test_the_projected_unknown_is_not_proof(self, script):
        """Closes the loop with the policy module: whatever "unknown" is spelled as,
        the reader must refuse it. A future change to UNKNOWN_PROVENANCE that made
        it a proven string would fail here rather than silently grant authority."""
        assert not is_proven(script.UNKNOWN_PROVENANCE)


# Marked per-class rather than module-wide: the reduction-rule and writer tests are
# synchronous, and a blanket `pytestmark` makes pytest-asyncio warn on each of them.
@pytest.mark.asyncio
class TestProjectionQuery:
    async def test_single_proven_row_projects_proven(self, script, db_url):
        await _seed(db_url, [{"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "oauth"}])

        assert await script.get_identity_provenance(db_url) == [
            {"provider_user_id": "123", "provider": "github", "methods": ["oauth"], "verification_method": "oauth"}
        ]

    async def test_self_asserted_row_is_projected_as_itself_not_upgraded(self, script, db_url):
        """The finding, in projection form. An unproven link must stay unproven."""
        await _seed(db_url, [{"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "self_asserted"}])

        result = await script.get_identity_provenance(db_url)

        assert result[0]["verification_method"] == "self_asserted"
        assert not is_proven(result[0]["verification_method"])

    async def test_legacy_magic_link_is_never_upgraded_to_confirmed(self, script, db_url):
        """Pre-#5664 rows are ambiguous and stay ambiguous.

        "Upgrading" them would be inventing evidence that was never collected —
        the script docstring's central promise, asserted rather than trusted.
        """
        await _seed(db_url, [{"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "magic_link"}])

        result = await script.get_identity_provenance(db_url)

        assert result[0]["verification_method"] == "magic_link"
        assert not is_proven(result[0]["verification_method"])

    async def test_disagreeing_rows_across_tenants_project_unknown(self, script, db_url):
        """One GitHub account, OAuth-proven in org-a, self-asserted in org-b."""
        await _seed(
            db_url,
            [
                {"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "oauth"},
                {"id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b", "method": "self_asserted"},
            ],
        )

        result = await script.get_identity_provenance(db_url)

        assert len(result) == 1
        assert result[0]["methods"] == ["oauth", "self_asserted"]
        assert result[0]["verification_method"] == script.UNKNOWN_PROVENANCE

    async def test_agreeing_rows_across_tenants_project_proven(self, script, db_url):
        """The legitimate multi-tenant case must NOT be degraded.

        An account OAuth-linked in both tenants has unambiguous provenance, and
        over-refusing here would deny real senders — the outage half of the
        trade-off this script exists to make safe.
        """
        await _seed(
            db_url,
            [
                {"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "oauth"},
                {"id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b", "method": "oauth"},
            ],
        )

        result = await script.get_identity_provenance(db_url)

        assert result[0]["verification_method"] == "oauth"

    async def test_provider_user_id_filter_selects_one_account(self, script, db_url):
        await _seed(
            db_url,
            [
                {"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "oauth"},
                {"id": "i2", "user_id": "u2", "pid": "456", "org_id": "org-b", "method": "self_asserted"},
            ],
        )

        result = await script.get_identity_provenance(db_url, provider_user_id="456")

        assert result == [{"provider_user_id": "456", "provider": "github", "methods": ["self_asserted"], "verification_method": "self_asserted"}]

    async def test_user_id_filter_still_reduces_over_the_accounts_full_set(self, script, db_url):
        """--user-id picks WHICH account to repair; the reduction is still total.

        Filtering the rows by user_id instead of the semi-join would reduce over
        u1's row alone and project "oauth" for an account that is self-asserted in
        another tenant — a targeted repair that manufactures proof.
        """
        await _seed(
            db_url,
            [
                {"id": "i1", "user_id": "u1", "pid": "123", "org_id": "org-a", "method": "oauth"},
                {"id": "i2", "user_id": "u2", "pid": "123", "org_id": "org-b", "method": "self_asserted"},
            ],
        )

        result = await script.get_identity_provenance(db_url, user_id="u1")

        assert result[0]["verification_method"] == script.UNKNOWN_PROVENANCE


class TestWriters:
    def _client_error(self, code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")

    def test_write_is_a_set_not_an_overwrite(self, script):
        """UpdateItem SET, so sibling attributes (member_org_ids, user_kind) survive.

        A PutItem here would blank the membership projection the cross-tenant
        trigger policy reads, turning a provenance backfill into an authorization
        regression on a different axis.
        """
        client = MagicMock()

        assert script.update_new_table(client, "123", "oauth", dry_run=False) is True

        kwargs = client.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"].startswith("SET verification_method")
        assert kwargs["ExpressionAttributeValues"][":vmethod"] == {"S": "oauth"}

    def test_write_refuses_to_create_a_bare_row(self, script):
        """UpdateItem upserts by default. A row carrying only provenance and no
        user_id would be an identity mapping Postgres never projected."""
        client = MagicMock()

        script.update_old_table(client, "123", "oauth", dry_run=False)

        assert "attribute_exists" in client.update_item.call_args.kwargs["ConditionExpression"]

    def test_absent_row_returns_false_not_success(self, script):
        client = MagicMock()
        client.update_item.side_effect = self._client_error("ConditionalCheckFailedException")

        assert script.update_new_table(client, "123", "oauth", dry_run=False) is False
        assert script.update_old_table(client, "123", "oauth", dry_run=False) is False

    def test_a_real_ddb_fault_raises_rather_than_reporting_success(self, script):
        """main() maps the raise to a non-zero exit, which is what the runbook's
        "do not republish the Lambda until the backfill is clean" step keys on."""
        client = MagicMock()
        client.update_item.side_effect = self._client_error("ProvisionedThroughputExceededException")

        with pytest.raises(ClientError):
            script.update_new_table(client, "123", "oauth", dry_run=False)

    def test_dry_run_writes_nothing(self, script):
        client = MagicMock()

        assert script.update_new_table(client, "123", "oauth", dry_run=True) is True
        assert script.update_old_table(client, "123", "oauth", dry_run=True) is True
        client.update_item.assert_not_called()
