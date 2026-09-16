"""V2 Bedrock rate storage: generations, variant-dimensioned rates, active pointer.

Issue #4969 (S2). Design note
`docs/design-notes/4969-openai-bedrock-pricing.md` §4.2.

What this migration is for
--------------------------

The legacy `model_pricing` table stores one row per model: an input rate, an
output rate, and nothing else. That shape cannot represent how Bedrock actually
prices OpenAI models, where the same model has different rates for
short/long context, in-region vs cross-region vs global routing, four service
tiers, GovCloud, and cache reads/writes. Squeezing that into one row per model is
why the wrong Sol rates went unnoticed: there was nowhere to put the right ones.

This migration adds three new tables alongside the legacy one and changes nothing
about it. `model_pricing` keeps its `model_id` primary key, its `NUMERIC(10,6)`
columns and every existing value, so an old writer's `ON CONFLICT (model_id)` and
an old reader's model-keyed dict both retain their exact original meaning through
the rollout. New OpenAI consumers ignore the legacy table entirely, including its
known-wrong `source='fallback'` OpenAI rows.

Three tables, and why each exists
---------------------------------

**`model_pricing_generations`** — a rate set is published as an atomic unit. A
generation is built in `status='building'`, validated against its own recorded
`required_variants` manifest, and only then marked `validated`. Nothing outside
the publishing transaction ever reads a building generation, so a half-fetched
refresh cannot be priced against.

**`model_pricing_rates_v2`** — one row per `(generation_id, model_id, geography,
service_tier, context_tier, region)`. Rates are `NUMERIC(14,10)`, not the legacy
`(10,6)`: scale 6 cannot hold Cyber's published 0.0171875 cache-write rate
(rounds to 0.017188) or Luna's GovCloud 0.0000264 cache read (rounds to
0.000026, a 1.52% error). Keeping generations side by side means a settlement
arriving after a publication can still read the generation it was priced under.

**`model_pricing_active`** — a single-row pointer naming the generation consumers
should read, plus a `pointer_revision` counter for optimistic concurrency, a
`consumers_enabled` flag so the rollout can seed data before anything reads it,
and `refresh_paused` so an operator rollback cannot be undone by an in-flight
refresh. Exactly one row exists, enforced by a `singleton BOOLEAN PRIMARY KEY
CHECK (singleton)`.

Constraints encode pricing policy, not just types
-------------------------------------------------

The CHECK constraints below are the same rules `pricing_policy.RateRow` enforces
in memory, deliberately duplicated at the storage layer so a row that would be
rejected by the package cannot be inserted by anything else either. The one worth
naming explicitly:

**Zero is not a synonym for unpublished.** `cache_write_policy='unpublished'`
requires a NULL price, `'no_additional_fee'` requires a price equal to the input
rate, and `'full_rate'` requires a non-null price. A zero stored where AWS
publishes no rate would make cached tokens look free and silently under-bill
them; a NULL is what lets a consumer say "charge these at the input rate and mark
the decision estimated".

Triggers and the trust boundary
-------------------------------

Triggers reject writes to a validated generation's rates or metadata, and reject
pointing the active pointer at a generation that is not validated or does not
cover its manifest.

These guards enforce the publisher's immutable-publication contract against
ordinary SQL writes. They are explicitly NOT a privilege boundary: both budget
Lambdas connect as the RDS master user (`infra/variables.tf` `rds_username`,
default `bgadmin`), which can disable triggers and issue DDL. This release
introduces no separate database role and makes no claim that an administrative
session cannot bypass them. Immutability rests on the trusted publisher following
the contract, backed by these guards and by tests.

Reversibility
-------------

`downgrade()` drops only objects this migration created. The legacy table is
untouched in both directions.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "044_model_pricing_v2"
down_revision: str | None = "043_person_anchor_rekey"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Dimension vocabularies. These MUST stay in step with the classes in
# pricing_policy/policy.py — the package validates the same values in memory.
_GEOGRAPHIES = ("in_region", "geo_cris", "global_cris", "govcloud")
_SERVICE_TIERS = ("standard", "priority", "flex", "batch")
_CONTEXT_TIERS = ("short", "long", "flat")
_CACHE_WRITE_POLICIES = ("full_rate", "no_additional_fee", "unpublished")
_SOURCES = ("bulk_catalog", "model_card", "bundled_snapshot")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    # ---------------------------------------------------------------- generations
    op.create_table(
        "model_pricing_generations",
        sa.Column("generation_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("required_variants", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("schema_version = 2", name="ck_generations_schema_version"),
        sa.CheckConstraint("policy_version > 0", name="ck_generations_policy_version"),
        sa.CheckConstraint("length(snapshot_version) > 0", name="ck_generations_snapshot_version"),
        sa.CheckConstraint(_in_list("status", ("building", "validated")), name="ck_generations_status"),
        # A validated generation must record WHEN it was validated, and a building
        # one must not pretend it already was. Without this, a crashed publisher
        # could leave a row that looks publishable.
        sa.CheckConstraint(
            "(status = 'validated' AND validated_at IS NOT NULL) OR (status = 'building' AND validated_at IS NULL)",
            name="ck_generations_validated_at_consistent",
        ),
        # The manifest is the coverage contract; an empty one would make the
        # pointer trigger's completeness check vacuously true.
        sa.CheckConstraint("jsonb_typeof(required_variants) = 'array'", name="ck_generations_manifest_is_array"),
        sa.CheckConstraint("jsonb_array_length(required_variants) > 0", name="ck_generations_manifest_nonempty"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_generations_content_sha256"),
    )
    op.create_index(
        "ix_model_pricing_generations_validated",
        "model_pricing_generations",
        ["status", "validated_at"],
    )

    # --------------------------------------------------------------------- rates
    op.create_table(
        "model_pricing_rates_v2",
        sa.Column("generation_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("geography", sa.String(32), nullable=False),
        sa.Column("service_tier", sa.String(16), nullable=False),
        sa.Column("context_tier", sa.String(16), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=True),
        sa.Column("input_price_per_1k_tokens", sa.Numeric(14, 10), nullable=False),
        sa.Column("output_price_per_1k_tokens", sa.Numeric(14, 10), nullable=False),
        sa.Column("cache_read_price_per_1k_tokens", sa.Numeric(14, 10), nullable=True),
        sa.Column("cache_write_price_per_1k_tokens", sa.Numeric(14, 10), nullable=True),
        sa.Column("cache_write_policy", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("source_effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_version", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["model_pricing_generations.generation_id"],
            name="fk_rates_v2_generation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "generation_id",
            "model_id",
            "geography",
            "service_tier",
            "context_tier",
            "region",
            name="pk_model_pricing_rates_v2",
        ),
        sa.CheckConstraint(_in_list("geography", _GEOGRAPHIES), name="ck_rates_v2_geography"),
        sa.CheckConstraint(_in_list("service_tier", _SERVICE_TIERS), name="ck_rates_v2_service_tier"),
        sa.CheckConstraint(_in_list("context_tier", _CONTEXT_TIERS), name="ck_rates_v2_context_tier"),
        sa.CheckConstraint(_in_list("cache_write_policy", _CACHE_WRITE_POLICIES), name="ck_rates_v2_cache_write_policy"),
        sa.CheckConstraint(_in_list("source", _SOURCES), name="ck_rates_v2_source"),
        sa.CheckConstraint("length(model_id) > 0 AND length(region) > 0", name="ck_rates_v2_identifiers_nonempty"),
        # Input and output are always charged, so a zero or negative rate is a
        # parse failure, not a price.
        sa.CheckConstraint("input_price_per_1k_tokens > 0", name="ck_rates_v2_input_positive"),
        sa.CheckConstraint("output_price_per_1k_tokens > 0", name="ck_rates_v2_output_positive"),
        # PostgreSQL orders NUMERIC NaN above every finite value, so > 0 alone
        # accepts it. Explicitly reject all non-finite representations.
        sa.CheckConstraint(
            "input_price_per_1k_tokens NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
            "AND output_price_per_1k_tokens NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
            "AND (cache_read_price_per_1k_tokens IS NULL OR "
            "cache_read_price_per_1k_tokens NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)) "
            "AND (cache_write_price_per_1k_tokens IS NULL OR "
            "cache_write_price_per_1k_tokens NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
            name="ck_rates_v2_finite",
        ),
        # Cache rates may legitimately be NULL (unpublished). If present, they must
        # be non-negative — a published zero is representable and distinct.
        sa.CheckConstraint(
            "cache_read_price_per_1k_tokens IS NULL OR cache_read_price_per_1k_tokens >= 0",
            name="ck_rates_v2_cache_read_non_negative",
        ),
        sa.CheckConstraint(
            "cache_write_price_per_1k_tokens IS NULL OR cache_write_price_per_1k_tokens >= 0",
            name="ck_rates_v2_cache_write_non_negative",
        ),
        # The cache-write policy tri-state. See the module docstring: zero is not a
        # synonym for unpublished, and conflating them under-bills silently.
        sa.CheckConstraint(
            "(cache_write_policy = 'unpublished' AND cache_write_price_per_1k_tokens IS NULL) "
            "OR (cache_write_policy = 'full_rate' AND cache_write_price_per_1k_tokens IS NOT NULL) "
            "OR (cache_write_policy = 'no_additional_fee' "
            "AND cache_write_price_per_1k_tokens IS NOT NULL "
            "AND cache_write_price_per_1k_tokens = input_price_per_1k_tokens)",
            name="ck_rates_v2_cache_write_policy_agrees",
        ),
        sa.CheckConstraint(
            "max_input_tokens IS NULL OR max_input_tokens > 0",
            name="ck_rates_v2_max_input_tokens_positive",
        ),
        # A flat-rate model has no context window boundary to record; a tiered one
        # without a maximum cannot have overflow detected.
        sa.CheckConstraint(
            "(context_tier = 'flat' AND max_input_tokens IS NULL) OR (context_tier <> 'flat' AND max_input_tokens IS NOT NULL)",
            name="ck_rates_v2_context_tier_bounds",
        ),
        sa.CheckConstraint("source_content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rates_v2_source_sha256"),
        sa.CheckConstraint("length(source_url) > 0", name="ck_rates_v2_source_url_nonempty"),
        # A seeded/retained row must say which bundle it came from, so a later
        # refresh can tell a bundled placeholder from a fetched rate.
        sa.CheckConstraint(
            "source <> 'bundled_snapshot' OR snapshot_version IS NOT NULL",
            name="ck_rates_v2_bundled_requires_version",
        ),
    )
    # The consumer's read path: every rate for one model in the active generation.
    op.create_index(
        "ix_rates_v2_generation_model",
        "model_pricing_rates_v2",
        ["generation_id", "model_id"],
    )
    # Staleness scans (§4.1: rows verified more than 48h ago are estimated).
    op.create_index("ix_rates_v2_verified_at", "model_pricing_rates_v2", ["verified_at"])

    # ------------------------------------------------------------ active pointer
    op.create_table(
        "model_pricing_active",
        sa.Column("singleton", sa.Boolean(), primary_key=True, server_default=sa.text("TRUE")),
        sa.Column("current_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("pointer_revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("consumers_enabled", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("refresh_paused", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["current_generation_id"],
            ["model_pricing_generations.generation_id"],
            name="fk_active_generation",
        ),
        sa.CheckConstraint("singleton", name="ck_active_singleton"),
        sa.CheckConstraint("pointer_revision >= 0", name="ck_active_pointer_revision"),
        # Enabling consumers with no generation to read would send every request
        # down the bootstrap path while reporting the rollout as complete.
        sa.CheckConstraint(
            "NOT consumers_enabled OR current_generation_id IS NOT NULL",
            name="ck_active_enabled_requires_generation",
        ),
    )

    # Exactly one pointer row, created here so no publisher has to race to insert
    # it. Consumers start disabled: 045 seeds the rates, and S6 activation flips
    # this flag only after the seed is verified present.
    op.execute(
        """
        INSERT INTO model_pricing_active
            (singleton, current_generation_id, pointer_revision, consumers_enabled, refresh_paused, updated_at)
        VALUES (TRUE, NULL, 0, FALSE, FALSE, now())
        """
    )

    _create_guards()


def _create_guards() -> None:
    """Triggers enforcing immutable publication (see the module docstring)."""

    # --- validated generations are frozen -----------------------------------
    # Rates may be written freely while a generation is 'building' and never
    # again once it is validated. This is what makes a published generation safe
    # to price against after the fact: a settlement reading generation A cannot
    # observe A changing underneath it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION model_pricing_rates_v2_immutable()
        RETURNS TRIGGER AS $$
        DECLARE
            target RECORD;
        BEGIN
            -- An UPDATE must protect its old generation as well as its new
            -- one: moving a row out is a mutation of the published rate set.
            -- SHARE locks also serialize against concurrent validation. A
            -- plain status read can see building and race a validation commit.
            FOR target IN
                SELECT generation_id, status
                  FROM model_pricing_generations
                 WHERE generation_id IN (
                     CASE WHEN TG_OP <> 'INSERT' THEN OLD.generation_id END,
                     CASE WHEN TG_OP <> 'DELETE' THEN NEW.generation_id END)
                 ORDER BY generation_id
                 FOR SHARE
            LOOP
                IF target.status = 'validated' THEN
                    RAISE EXCEPTION
                        'model_pricing_rates_v2 is immutable for validated generation % (attempted %)',
                        target.generation_id, TG_OP
                        USING ERRCODE = 'raise_exception';
                END IF;
            END LOOP;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_model_pricing_rates_v2_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON model_pricing_rates_v2
        FOR EACH ROW EXECUTE FUNCTION model_pricing_rates_v2_immutable()
        """
    )

    # --- validated generation metadata is frozen, except the one legal transition
    # building -> validated. Everything else about the row must stay put, so the
    # recorded manifest and content hash cannot be rewritten to match rates that
    # were changed afterwards.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION model_pricing_generations_immutable()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'validated' THEN
                    RAISE EXCEPTION 'validated pricing generation % cannot be deleted', OLD.generation_id
                        USING ERRCODE = 'raise_exception';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status = 'validated' THEN
                RAISE EXCEPTION 'validated pricing generation % is immutable', OLD.generation_id
                    USING ERRCODE = 'raise_exception';
            END IF;

            IF NEW.schema_version    IS DISTINCT FROM OLD.schema_version
            OR NEW.policy_version    IS DISTINCT FROM OLD.policy_version
            OR NEW.snapshot_version  IS DISTINCT FROM OLD.snapshot_version
            OR NEW.required_variants IS DISTINCT FROM OLD.required_variants
            OR NEW.content_sha256    IS DISTINCT FROM OLD.content_sha256
            OR NEW.created_at        IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION
                    'pricing generation % may only transition status, not rewrite its manifest or provenance',
                    OLD.generation_id
                    USING ERRCODE = 'raise_exception';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_model_pricing_generations_immutable
        BEFORE UPDATE OR DELETE ON model_pricing_generations
        FOR EACH ROW EXECUTE FUNCTION model_pricing_generations_immutable()
        """
    )

    # --- the pointer may only name a validated, fully-covered generation ----
    # The completeness check compares the generation's actual rows against the
    # manifest it recorded at build time. A generation missing a long-context or
    # GovCloud key would otherwise become active and silently price those requests
    # off a fallback row.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION model_pricing_active_guard()
        RETURNS TRIGGER AS $$
        DECLARE
            target_status TEXT;
            missing_count INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'the model_pricing_active pointer row cannot be deleted'
                    USING ERRCODE = 'raise_exception';
            END IF;

            IF NEW.current_generation_id IS NOT NULL
               AND NEW.current_generation_id IS DISTINCT FROM OLD.current_generation_id THEN

                SELECT status INTO target_status
                  FROM model_pricing_generations
                 WHERE generation_id = NEW.current_generation_id;

                IF target_status IS NULL THEN
                    RAISE EXCEPTION 'pricing generation % does not exist', NEW.current_generation_id
                        USING ERRCODE = 'raise_exception';
                END IF;

                IF target_status <> 'validated' THEN
                    RAISE EXCEPTION
                        'pricing generation % is %, not validated; refusing to activate it',
                        NEW.current_generation_id, target_status
                        USING ERRCODE = 'raise_exception';
                END IF;

                -- Every manifest key must be present as an actual rate row.
                SELECT count(*) INTO missing_count
                  FROM (
                    SELECT manifest.key
                      FROM model_pricing_generations g
                      CROSS JOIN LATERAL jsonb_array_elements(g.required_variants) AS manifest(key)
                     WHERE g.generation_id = NEW.current_generation_id
                  ) required
                 WHERE NOT EXISTS (
                    SELECT 1
                      FROM model_pricing_rates_v2 r
                     WHERE r.generation_id = NEW.current_generation_id
                       AND r.model_id     = required.key->>0
                       AND r.geography    = required.key->>1
                       AND r.service_tier = required.key->>2
                       AND r.context_tier = required.key->>3
                       AND r.region       = required.key->>4
                 );

                IF missing_count > 0 THEN
                    RAISE EXCEPTION
                        'pricing generation % is missing % required variant key(s); refusing to activate it',
                        NEW.current_generation_id, missing_count
                        USING ERRCODE = 'raise_exception';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_model_pricing_active_guard
        BEFORE UPDATE OR DELETE ON model_pricing_active
        FOR EACH ROW EXECUTE FUNCTION model_pricing_active_guard()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_model_pricing_active_guard ON model_pricing_active")
    op.execute("DROP FUNCTION IF EXISTS model_pricing_active_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_model_pricing_generations_immutable ON model_pricing_generations")
    op.execute("DROP FUNCTION IF EXISTS model_pricing_generations_immutable()")
    op.execute("DROP TRIGGER IF EXISTS trg_model_pricing_rates_v2_immutable ON model_pricing_rates_v2")
    op.execute("DROP FUNCTION IF EXISTS model_pricing_rates_v2_immutable()")

    op.drop_table("model_pricing_active")
    op.drop_index("ix_rates_v2_verified_at", table_name="model_pricing_rates_v2")
    op.drop_index("ix_rates_v2_generation_model", table_name="model_pricing_rates_v2")
    op.drop_table("model_pricing_rates_v2")
    op.drop_index("ix_model_pricing_generations_validated", table_name="model_pricing_generations")
    op.drop_table("model_pricing_generations")
