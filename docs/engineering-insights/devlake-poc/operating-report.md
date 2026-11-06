# DevLake POC S07 operating report — live verification pending

This public report is a status and evidence checklist, **not** a live pilot
receipt. No 48-hour soak, fault injection, upgrade/rollback or cross-scope
storage probe has been verified here. A passing unit fixture or privately
supplied receipt does not change the live outcome. Production HA and scale
certification are outside this POC.

| Acceptance | Live outcome | Revision / run / attempts | Private evidence and cleanup still required |
| --- | --- | --- | --- |
| AC-01 — freshness and bounds | Incomplete | Not observed | Frozen 20-update sample across at least 48 hours; independent native timestamps and authorized reads; p95 at most 15 minutes; query distribution, completeness/gaps, resource ceilings, billed versus estimated cost, cleanup. |
| AC-02 — recovery and controls | Incomplete | Not observed | Source/restart/revocation barriers, B1 bytes across B2, every DB client's IAM renewal, optional modes, retained Profile and holds, each terminal finalization attempt, independently checked cleanup. |
| AC-03 — compatibility and rollback | Incomplete | Not observed | Pinned upstream/images/schema and patch comparison, migration-required denial before provider work, migrator DML and worker DDL denials, restored-image Profile/evidence/shared-state receipt, cleanup. |
| AC-04 — cell and visibility isolation | Incomplete | Not observed | Two tenants, two same-tenant Projects and shared/private collision, concurrent collection, real DB/S3/KMS role denials, revocation barrier, unchanged shared response bytes, cleanup. |

The acceptance IDs correspond to scenario selectors SCN-01 through SCN-04.
Their offline outputs remain separate from the live outcomes above. Required
missing or failed observations remain visible as `incomplete`, `unavailable`,
`not_run` or `fail`; no missing proof is inferred as a pass. A budget or
billing observation not yet obtained is unknown, not zero.

## Private evidence index

The verification engineer uses the operator-owned campaign directory, not
this public file, to retain revisions, run IDs, attempts and evidence pointers.
With an authorized private mount and pinned launch inputs, run:

`python3 modules/insights/poc/devlake/operating_report.py --root <private-absolute-root> --campaign-id <approved-id> --revision <pinned-revision>`

The command writes mode-0600 `operating-report.json` beside the campaign
checkpoint. Each AC row has its pinned revision and run ID, attempt IDs,
private evidence references, offline status, required missing live proof and
cleanup status; freshness and operating measurements are retained separately.
It re-evaluates the pinned checkpoint and refuses a changed or inconsistent
source report. It can index evidence after launch authority expires, but never
claims live acceptance or emits private references to stdout. Do not commit
that JSON, source identities, resource IDs, real URLs or operator profiles.

After an authorized live campaign, the S01 verification engineer and component
owners must separately record actual run revisions, independent observations,
attempts, approved holds and owned cleanup in their protected evidence store.
Only a compatible, independently checked live verdict may update this public
status; a PR publication, merge or synthetic check does not do so.
