"""Replace copied secret ARNs with ADP credential references.

Issue #5046 (U13b), EPIC #4910 — R7 schema half.

WHAT THIS MIGRATION CHANGES

``credential_registry.secret_arn`` -> ``credential_registry.adp_credential_id``, and
``cloud_accounts.secret_arns_json`` -> ``cloud_accounts.adp_credential_ids_json``.
``credential_registry.kms_key_id`` is dropped outright.

A secret's ARN is its address in AWS Secrets Manager. Storing a copy of that address in a
domain record creates a second, independent route to the secret material that lives
outside the vault, so vault rotation and revocation stop reaching it. ``kms_key_id`` goes
with it because it existed only to decrypt a secret this record no longer resolves.

The retained ``cross_account_role_arn`` / ``ingest_role_arn`` / ``irsa_role_arns_json``
columns are IAM *role* ARNs — identities that name who may act, carrying no secret value.
They are a different class of reference and are deliberately untouched.

WHY THIS MIGRATION CAN REFUSE TO RUN

The requirement is that mapping existing rows onto ADP credential IDs must *establish, not
assume*, that each mapped credential is ADP-owned and ADP-accessible, that account and KMS
permissions permit ADP to read it, and that rotation and revocation work through the new
reference (design section 6). None of that is checkable from inside a schema migration: it
has no vault access, and Superplane is deliberately not given broad read access to ADP
secrets.

So this migration does not attempt a value mapping. An ARN and an ADP credential ID are
not translations of each other, and deriving one from the other would produce records
pointing at credentials nobody confirmed ADP can read — a silent failure whose blast
radius is exactly the "migration assumes ADP ownership" row of the issue's risk table.

Instead it classifies the database it is run against:

* **supported** — no ``credential_registry`` rows and no populated
  ``cloud_accounts.secret_arns_json``: nothing references secret material yet, so the
  cutover is a pure schema change and applies.
* **unknown** — any such row exists: **refuse**, naming the audited vault-owned migration
  that has to run first. Refusing is the designed behavior, not a shortcoming. It leaves
  the deployed database exactly as it was, upgradeable once the audited run has produced
  verified references.

That audited run is R7 acceptances 6-7. It is deferred live work owned by U7 and is NOT
closed by this revision.

Offline (``--sql``) mode cannot inspect rows at all — it never opens a connection — so the
state check is skipped there and the DDL renders unconditionally. A rendered script is
therefore not evidence that any particular database is in the supported state; the check
runs when the migration is actually applied.

ROLLBACK

``downgrade`` restores the dropped columns as **nullable and empty**. It must never
repopulate a secret ARN, because the prior column held an address this revision did not
create: re-copying it would silently recreate the second reference the upgrade removed.
The old values are recoverable only from the operator's pre-migration backup, which is
what the issue's rollback/data-preservation requirement asks be retained.

CHAIN POSITION

This revision was authored as ``011`` on top of ``010_add_workspace_grants``, the head at
the time. U15 (#5387) then merged its own ``011_add_observation_receiver_tables`` onto that
same parent. Two revisions sharing a parent is not a merge conflict Git can see -- both
files simply exist -- but it makes the chain two-headed, and ``alembic upgrade head`` then
aborts on the ambiguity without applying *anything*, U15's revision included. That is
strictly worse than this revision's deliberate refusal, which is targeted and leaves a
recoverable database.

So it is renumbered to ``012`` and re-parented onto U15's revision, keeping the chain the
single line that #5045 (U13) established. Renumbering is free here because nothing has been
applied to any database yet: ``schema.status`` in ``releases/superplane.lock.yaml`` is still
``unverified``, and the content-hash freeze in ``tests/test_migrations.py`` covers revisions
001-005 only. The two revisions are independent -- U15 adds observation tables, this one
alters credential columns -- so the order between them carries no requirement either way.

Revision ID: 012_adp_credential_reference
Revises: 011_add_observation_receiver_tables
"""

import sqlalchemy as sa
from alembic import op

revision = "012_adp_credential_reference"
down_revision = "011_add_observation_receiver_tables"
branch_labels = None
depends_on = None


# Raised instead of a bare RuntimeError so an operator (and the tests) can distinguish a
# deliberate refusal from a genuine migration failure. A refusal means "this database
# needs the audited vault-owned migration first"; a failure means something is broken.
class UnverifiedCredentialReferencesError(RuntimeError):
    """The database holds secret ARNs that no audited vault migration has remapped."""


_REFUSAL = (
    "Refusing to migrate credential references: this database holds {n} record(s) that "
    "still reference secret material by ARN ({detail}). An ADP credential ID cannot be "
    "derived from a secret ARN -- doing so would point these records at credentials that "
    "nobody has established ADP owns, can read under the relevant account and KMS "
    "permissions, or can rotate and revoke through the new reference. That verification "
    "is the audited vault-owned migration (issue #5046, R7 acceptances 6-7, owned by U7), "
    "which must run first and record a verified ADP credential ID per row. No schema "
    "change has been made; this database is unchanged and still upgradeable."
)


def _refuse_if_unverified_references_exist() -> None:
    """Classify the target database, and refuse on anything but the supported state.

    Skipped in offline mode, where there is no connection to inspect. `op.get_bind()`
    returns a mock connection under `--sql`, so issuing a SELECT there would either fail
    or -- worse -- appear to return no rows and wave through a database that actually has
    them. Checking `as_sql` first makes that impossible.
    """
    context = op.get_context()
    if context.as_sql:
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    problems: list[str] = []
    total = 0

    # A pre-012 credential_registry row cannot already contain a verified ADP credential
    # ID: the column does not exist until this revision creates it. Even an empty legacy
    # ARN therefore represents an unmapped row, not a verified reference. Refuse every
    # row before any DDL so the audited vault-owned migration retains the old record and
    # can establish a real mapping through an explicit channel.
    if "credential_registry" in tables:
        count = bind.execute(
            sa.text("SELECT count(*) FROM credential_registry")
        ).scalar_one()
        if count:
            total += count
            problems.append(f"credential_registry: {count} row(s)")

    if "cloud_accounts" in tables:
        columns = {c["name"] for c in inspector.get_columns("cloud_accounts")}
        if "secret_arns_json" in columns:
            # `'[]'` is an empty list, not a reference to anything, so it is not a blocker.
            count = bind.execute(
                sa.text(
                    "SELECT count(*) FROM cloud_accounts "
                    "WHERE secret_arns_json IS NOT NULL "
                    "AND secret_arns_json NOT IN ('', '[]')"
                )
            ).scalar_one()
            if count:
                total += count
                problems.append(f"cloud_accounts.secret_arns_json: {count} row(s)")

    if problems:
        raise UnverifiedCredentialReferencesError(
            _REFUSAL.format(n=total, detail="; ".join(problems))
        )


def upgrade() -> None:
    _refuse_if_unverified_references_exist()

    # --- credential_registry: secret ARN + KMS key -> opaque ADP credential reference ---
    #
    # Added nullable, then made NOT NULL to match the model. The precondition above proves
    # there are no rows to backfill. In particular, do not manufacture an empty credential
    # ID: the model correctly rejects it because it cannot resolve to an ADP vault record.
    op.add_column(
        "credential_registry",
        sa.Column("adp_credential_id", sa.String(255), nullable=True),
    )
    # `batch_alter_table` rather than a bare `alter_column`, because a bare one renders
    # `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL` -- PostgreSQL syntax that SQLite has
    # no equivalent for, so it raised `OperationalError: near "ALTER": syntax error` and
    # took down every behavioral test of this revision, including the two that assert the
    # secret-ARN and KMS columns are actually gone. The batch form emits the same ALTER on
    # PostgreSQL and falls back to a table rebuild on SQLite, so the suite that proves this
    # migration's security property can run on the backend the tests use.
    with op.batch_alter_table("credential_registry") as batch_op:
        batch_op.alter_column(
            "adp_credential_id",
            existing_type=sa.String(255),
            nullable=False,
        )

    op.drop_column("credential_registry", "secret_arn")
    op.drop_column("credential_registry", "kms_key_id")

    # --- cloud_accounts: list of secret ARNs -> list of ADP credential IDs ---
    op.add_column(
        "cloud_accounts",
        sa.Column("adp_credential_ids_json", sa.Text, nullable=True),
    )
    op.drop_column("cloud_accounts", "secret_arns_json")


def downgrade() -> None:
    # Restored nullable and left EMPTY on purpose. See the module docstring: repopulating
    # a secret ARN here would recreate the unmanaged second reference to secret material
    # that the upgrade exists to remove. Recover prior values from the pre-migration
    # backup if they are genuinely needed.
    op.add_column(
        "cloud_accounts", sa.Column("secret_arns_json", sa.Text, nullable=True)
    )
    op.drop_column("cloud_accounts", "adp_credential_ids_json")

    op.add_column(
        "credential_registry", sa.Column("kms_key_id", sa.String(512), nullable=True)
    )
    # Nullable, unlike the original NOT NULL column: there is no value to put in it, and a
    # NOT NULL restore would either fail or force a fabricated ARN into every row.
    op.add_column(
        "credential_registry", sa.Column("secret_arn", sa.String(512), nullable=True)
    )
    op.drop_column("credential_registry", "adp_credential_id")
