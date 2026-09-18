# Deployed-state inventory for the domain schema

Issue #5045 (U13), EPIC #4910. This is the **discovery result** that R4 acceptance 2 calls
for: the enumeration of the already-deployed database states this repository must support
upgrading *from*, and how each is detected.

It is recorded as a document rather than as upgrade migrations because of what the
discovery found.

## Result: the inventory is empty

**No deployed domain database exists, and none can have been created by this chain.** Three
independent lines of evidence, each checkable from this repository:

1. **The migration runner has never existed as an artifact.**
   `releases/superplane.lock.yaml` records `superplane-api` under `pending_images` with no
   `digest` and `blocked_by: no build has run yet`. The Alembic chain ships *inside* that
   image, so the image **is** the runner. `tests/test_lock.py` asserts pending entries carry
   no digest, so this cannot be quietly fabricated.

2. **The migration lane has never applied anything.**
   `.github/workflows/superplane-migrate.yml` is `workflow_dispatch`-only and calls
   `infra/scripts/check_migration_contract.py`, which refuses before touching a database
   when the lock reports no single head or no runner digest. Both refusals were active for
   the whole life of this chain.

3. **The decisive one: the chain was never loadable.**
   Before this story, Alembic's own `ScriptDirectory` raised `KeyError: '005'` while
   building its revision map — three files declared a parent of `"005"`, an id no file
   declares. A chain that cannot be parsed cannot be applied, so `alembic upgrade` could
   never have written *any* revision into an `alembic_version` table — not even `001`.

Point 3 is what makes this an inventory rather than an assumption. Points 1 and 2 establish
that no ADP-managed deploy ran; point 3 establishes that no deploy *anywhere* could have
applied this chain, including one this repository has no visibility into.

## What follows for the upgrade path

The issue's upgrade half asks for an expand/backfill/validate/contract path per deployed
state, and requires that state fixtures be **captured from real deployed databases, never
hand-written from the models** — because "a fixture written from the models cannot represent
a state the models never produced".

With an empty inventory there is nothing to capture from. Writing upgrade migrations anyway
would mean inventing the starting states, which is the specific failure the fixture rule
exists to prevent, and would ship untested migrations against databases nobody has observed
— one of the blast-radius rows in the issue's own risk table ("Unknown starting state
processed best-effort").

So this story implements the **fresh-install half** and records the upgrade half as
inapplicable-for-now rather than as done.

## If a database is later found to exist

The premise above is falsifiable, and this is the check that falsifies it. Against the
target database:

```sql
SELECT version_num FROM <domain schema>.alembic_version;
```

- **Relation does not exist** — never migrated. The fresh-install path applies; this is the
  expected result.
- **Any row at all** — the premise here is wrong and this document is stale. Do **not** run
  `alembic upgrade head` against it. A bare `006` or `007` in that column is ambiguous by
  construction (three files declared each id before the repair), so the stamped revision
  cannot be identified from `alembic_version` alone and needs a schema probe — which columns
  and indexes are actually present — to determine what ran. Reopen the upgrade half with
  that real state as its first inventoried fixture.

Rejecting an uninventoried state with a diagnostic naming what was found, rather than
processing it best-effort, is the behaviour the issue requires.

## Applied-revision immutability

Revisions `001`–`005` were left byte-identical by this story and are pinned by content hash
in `src/superplane-api/tests/test_migrations.py`. They are the only revisions whose ids were
unambiguous, so they are the only ones a database could have recorded unambiguously; that
test fails if a future change rewrites one.

Every id this story rewrote belonged to a file that was unreachable by construction
(duplicate id or dangling parent), so no recorded revision history can reference it.

## Live acceptance that remains open

Neither of these closes with this story, and both are recorded as deferred in the issue:

- **The live smoke check** — `alembic upgrade head` against an ephemeral empty PostgreSQL
  exiting `0`. The offline equivalent that *has* run is
  `alembic upgrade head --sql`, which resolves a single head and compiles every migration
  for the PostgreSQL dialect but never opens a connection.
- **R4 acceptance 3** — the data-preserving upgrade against a real deployed database, with
  a backup taken **and its restore exercised**. Gated on a named account/environment,
  authorized database access, a backup target and a named cleanup owner, all unresolved.

`releases/superplane.lock.yaml` therefore keeps `schema.status: unverified` even though
`schema.single_head` is now `true`: single-headedness is a property of the files and is
established offline, while `verified` is a property of a real database and is not.
