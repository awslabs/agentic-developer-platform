# Real-PostgreSQL migration tests

Most tests in this directory run against SQLite. The V2 pricing tests
(`test_044_model_pricing_v2.py`, `test_045_pricing_seed_parity.py`) cannot: they
assert on `NUMERIC(14,10)` precision, `JSONB`, `GENERATED ALWAYS AS IDENTITY`,
plpgsql triggers, `SELECT ... FOR UPDATE` row locking, and the PostgreSQL error
codes `42P01`/`42703` that the feature-detecting readers depend on. None of that
exists in SQLite, so a SQLite run would report green while testing none of the
behaviour that matters. The design note for #4969 therefore requires "isolated
PostgreSQL 16 with actual migrations, not SQLite".

## How the server is provided

`conftest_postgres.py` uses the [`pgserver`](https://pypi.org/project/pgserver/)
package, which bundles a real PostgreSQL 16 binary and runs it against a
temporary data directory over a unix socket. No Docker, no root, no port bound on
the host. It is a test-only dependency, declared in the `dev` extra.

Each test gets a freshly created database on one session-scoped server, so
trigger state, sequences and identity counters cannot leak between tests —
identity values are part of what the generation-id tests assert on.

## Running them

`pgserver` publishes wheels for **Python 3.12 and older only**. The repo's main
test virtualenv is 3.13, so a second one is used for these tests:

```bash
cd modules/gateway
uv venv --python 3.12 .venv312
uv pip install --python .venv312 -e '.[dev]'
.venv312/bin/python -m pytest tests/migrations/ -q
```

On an interpreter without `pgserver` (including 3.13) the fixtures **skip**;
they do not fail. The skip is raised inside the fixtures rather than at module
import time on purpose — a module-level `pytest.importorskip` in a conftest
aborts collection for the entire directory and would silently take the
SQLite-based migration tests with it.

CI runs these tests: the Test job in `.github/workflows/gateway-ci.yml` uses
Python 3.12, and the dependency's environment marker (`python_version < '3.13'`)
installs there.

## The pgcrypto shim

Migration `016_action_provenance.py` runs `CREATE EXTENSION IF NOT EXISTS
pgcrypto`. RDS ships pgcrypto; the pgserver build does not (its extension
directory contains only `plpgsql` and `vector`), so without help the whole chain
aborts at 016 and nothing downstream can be tested at all.

`_ensure_pgcrypto_shim()` installs a minimal `pgcrypto.control` plus a SQL body
that only asserts `gen_random_uuid()` resolves. That is sound, not merely
expedient: 016 wants pgcrypto solely for `gen_random_uuid()`, which has been in
PostgreSQL core since 13, and nothing in `alembic/` or `src/` calls any other
pgcrypto function (no `digest`/`crypt`/`gen_salt`/`hmac`/`pgp_*`). If a future
PostgreSQL ever drops the core function, `CREATE EXTENSION` fails loudly instead
of letting a migration produce NULL ids.

The shim writes into the installed `pgserver` package only — never into the repo
— and changes nothing about production, where the real extension is present.
