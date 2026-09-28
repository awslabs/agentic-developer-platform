# Refresh scratch and persistent state

`refresh-repos.py` allocates a unique scratch directory for each ingestion
subprocess and each incremental wiki diff. `SCRATCH_BASE` defaults to `/tmp`;
the resolved path must remain beneath `/tmp` and outside configured persistent
state/source roots. The refresh manifest supplies a 10Gi `/tmp` emptyDir and a
job deadline. The Python setting alone is not a disk quota. Never mount shared
S3 storage beneath this scratch root. Repository names must be exactly two safe
path components; `.github` repositories remain supported.

Scratch is removed in `finally` after success, exceptions, subprocess timeout or
Python cancellation. This is not a promise of cleanup after SIGKILL, machine
failure or a killed process that cannot execute Python cleanup; the disposable
pod volume supplies that lifecycle boundary. Child-process-tree termination
and actual CSI behavior require runtime verification.

A failed ingestion process does not advance `last_sha` or `code_index_sha`, so
the next refresh retries. A failed Git diff is distinct from an empty diff.
State publication retains the existing complete-object `open(..., "w")`, JSON
write, close sequence. It does not append, rename, lock or update random offsets
on Mountpoint. Concurrent scratch is isolated; shared state remains a complete
snapshot and is not a transactional concurrent-writer database. The deployed
refresh CronJob's concurrency policy must remain part of rollout validation.

## Shared clone setting audit

- `refresh-repos.py`: both Git execution paths use unique scratch. The child
  receives an explicit `CLONE_BASE` override; it is cleaned up by the parent.
- `ingest-repo.py`: direct invocation still derives its clone destination from
  `CLONE_BASE`. It also decides persistence from a `/tmp` string prefix. This
  excluded source belongs to parser continuation #6059 and needs its own strict
  scratch/source-artifact contract. This PR does not make direct default
  ingestion on `/platform-data/repos` safe.
- `ingestion-scaledjob.yaml`: already overrides `CLONE_BASE=/tmp/repos` for
  workers. Unique attempts and parser isolation are owned by #6059.
- `generate-learning-artifacts.py`: reads wiki/source previews from existing
  persistent clone snapshots (and a legacy wiki fallback); it does not perform
  Git operations. Keeping `CLONE_BASE` unchanged preserves these existing
  lookups. This does not guarantee fresh snapshots from ephemeral ingestion.
- `lint-wiki.py`: checks learning-path file references against that existing
  source snapshot. It remains read-only. Fresh source publication and avoiding
  stale/missing snapshots need the #6059 fetch/publication handoff.
- `discover-infra.py`: the existing regression tests explicitly prohibit
  iterating persistent clone directories; it reads the configured repo list.

The shared default remains unchanged to avoid silently redirecting read-only
consumers to disposable paths. The remaining direct-ingest issue is explicit,
not closed by this continuation. No source snapshot replication, tenant prefix,
ACL or authority changes are included.

## Evidence and limits

`tests/test_refresh_scratch.py` imports the actual scripts with cloud/model
adapters disabled. It executes real local Git clone/diff, actual incremental
wiki publication through an in-memory store, and actual refresh state logic
through a local ingestion-process adapter. The adapter checks clone placement
and emits a synthetic index result; it is not proof of the separately owned
parser, database or provider internals. Existing actual-image lexical/SCIP
acceptance belongs to the nonroot-image gate, not this fixture.

The persistent contract guards production state opens/writes and rejects Git
at the process destination, in-place modes, seeking and rename. The exact old
persistent Git helper destination fails under the same guard; the new scratch
path succeeds. Tests cover state readback/overwrite, failed-ingest retry,
concurrent attempts, timeout/cancellation cleanup, path refusal and actual
learning/lint reads of an existing snapshot. They do not run the S3 CSI driver.

Before live acceptance, verify Mountpoint UID/modes and supported full-object
overwrite, all shared readers, immutable rollback images and each deployed
consumer. No image promotion, live rollout or storage mutation is performed by
this source PR. S15 #5614 / #4720 remains open.
