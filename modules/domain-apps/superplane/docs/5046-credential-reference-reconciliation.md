# Credential-reference schema reconciliation (#5046)

Superplane stores an opaque ADP credential ID, not a copy of a secret value or its
AWS secret ARN. This is the **offline schema half** of the credential contract;
passing reference-shape validation does not prove that ADP owns the credential,
can read it with the target account/KMS permissions, or can rotate or revoke it.

The September 2026 issue audit described a then-open encoded-reference bypass.
That description is stale for current source: repair #5465 merged the bounded
fixed-point decoder while the older #5462 PR remained open. This reconciliation
checks the merged behavior rather than porting the superseded repair or adding
another migration.

| Criterion | Current-source result | Evidence and remaining limit |
| --- | --- | --- |
| Accept an opaque ID; refuse an ARN or secret value, including chained encodings | The shared validator is used by credential, account-list and provider-connection models and account request schemas. HTTP write routes return scrubbed 422s; valid opaque IDs are stored unchanged. | `src/superplane-api/tests/test_models.py` and `tests/test_accounts.py` test plain, JSON-unicode, HTML, percent and confusable input, budget exhaustion, valid IDs and non-persistence. The validator checks shape, not vault availability. |
| No copied secret-ARN column or active domain-record reader | Credential, cloud-account and provider-connection tables use ADP ID fields. A source-tree regression rejects reads of legacy secret-ARN model attributes. | `tests/test_models.py` checks model fields and active Python readers; `tests/test_vault_sync.py` checks that the old delivery path reports unavailable. IAM **role** ARNs remain: they identify a principal, not a secret. An unwired ExternalSecret helper still accepts a secret-ARN parameter; it is not an active record reader or approved delivery path. |
| Supported upgrades preserve rows; unknown legacy state refuses | Existing revision `012_adp_credential_reference` changes only the supported state and refuses legacy credential rows or populated secret-ARN lists before DDL. Rollback leaves restored ARN columns empty. | `tests/test_migrations.py` checks populated account row counts, surviving identity fields, absent copied lists, untouched legacy rows/schema on refusal, rollback and PostgreSQL SQL rendering. `tests/test_lock.py` checks head/lock parity. SQLite application and PostgreSQL SQL rendering do **not** prove a live PostgreSQL upgrade. |
| Model/migration change coverage at least 85% | CI-style path-based coverage on the current source: migration 012 **100%**, credential model **98%**, cloud-account model **93%** (260 focused tests passed on Python 3.12). | Coverage is statement coverage of these files in the offline fixture, not live credential usability. Final-head API/domain CI results are reported on the PR separately. |
| Audited migration, rotation and revocation (R7 acceptances 6–7) | **Pending with U7/vault owners**, not closed by this offline reconciliation. | Requires an authorized named account, checked vault/KMS access, exact mapping, backup/recovery and cleanup owner. No live database or cloud resource was changed here. Installed-image verification belongs to #5538; release belongs to #5327. |

To reproduce the focused checks, use Python 3.12 and install the API's maintained
local-package dependencies as specified by `.github/workflows/superplane-domain-ci.yml`.
From `modules/domain-apps/superplane/src/superplane-api` run:

```bash
python -m pytest tests/test_models.py tests/test_migrations.py tests/test_accounts.py -q
```

Run the module lock checks separately from that same directory:

```bash
python -m pytest ../../tests/test_lock.py -q
```

The maintained PR gate is `.github/workflows/superplane-domain-ci.yml` (its API
job and domain checks), not the older upstream workflow name in the historical
issue text. Keep the release lock's schema status `unverified` until its separate
real-database acceptance has been established; do not recover a prior secret ARN
by copying it into a downgraded domain record.
