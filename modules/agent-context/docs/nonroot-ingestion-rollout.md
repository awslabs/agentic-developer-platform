# Ingestion non-root image: validation, rollout and rollback

This source change prepares five consumers: ingestion-scaledjob, repo-refresh-cronjob,
vuln-scan-cronjob, personal-context-synthesis-cronjob and migration-job. It does not
establish that the deployed workloads already use these restrictions.

## Exact-source local gate

From the repository root, after committing the reviewed source:

```sh
python3 modules/agent-context/scripts/validate-ingestion-image.py \
  --revision <reviewed-commit-sha> --output /tmp/ingestion-validation-<unique-id>
```

The gate archives only tracked ingestion, pipeline, Alembic, personal_context and
validation source. It stages the maintained external package inputs into a clean
build context, matching production CodeBuild. It uses an empty Docker client config
and the local daemon, and forwards no host credentials or secret mounts. Build-time
public package downloads follow the existing Dockerfile versions; nothing is pushed.
The receipt records the exact source archive checksum and resulting image ID.

Runtime acceptance uses that image ID with UID/GID 1001, read-only root, ALL
capabilities dropped, no-new-privileges, Docker's default seccomp filter (asserted
active), network none, bounded tmp/home mounts and a newly created disposable volume.
The separate root fixture setup has only CHOWN capability and touches that new volume.
The actual runtime verifies five packaged entrypoints (Alembic offline SQL), immutable
application/tools, intended artifact writes, preserved unrelated fixture ownership,
real Chromium rendering, real Zoekt/basic lexical output, real Python SCIP output,
local-proxy Go module/cache/build capability, and a planted-tool marker refusal.
Missing Docker, failed build, missing browser, import failure or refused positive
capability is a failing gate, not a skip. This remains local image compatibility;
it does not prove production IAM, DB operations, CNI or tenant isolation.

## Writable paths

| Consumer | Writable paths and purpose |
|---|---|
| Queue worker | /tmp clones/output; /home/appuser caches; approved /platform-data repos, code-indexes, learning and state artifacts |
| Repository refresh | approved /platform-data repos, code-indexes, learning and state; /tmp intermediate output; /home/appuser tool caches |
| Vulnerability scanner | /tmp downloaded SBOMs and intermediate files; /home/appuser/.cache for scanner caches |
| Personal synthesis | /tmp and /home/appuser scratch only; remote storage operations are outside local validation |
| Migration | /tmp and /home/appuser temporary/cache files; packaged /app/alembic remains immutable |

HOME, XDG_CACHE_HOME, NPM_CONFIG_CACHE, GOMODCACHE and GOCACHE point to bounded
writable home. GOPATH stays /opt/go so installed scip-go remains protected;
GOMODCACHE explicitly overrides its normally read-only default. Python bytecode
writes are disabled. Chromium binaries stay root-owned in /opt/browsers.

## PVC ownership preflight: supervisor-owned, no blanket recursive migration

Before rollout, inventory the actual storage class/CSI fsGroup implementation and
all consumers of platform-data. Snapshot directory ownership, group, mode, ACLs and
counts/checksums for the specific ingestion write roots and unrelated data. Identify
existing root-owned files that require updates; a group change alone does not grant
write permission. Do not run chgrp/chmod recursively on /platform-data.

The two PVC consumers declare fsGroupChangePolicy: OnRootMismatch. This avoids the
standard kubelet recursive permission pass only when the volume root already matches
the required group/mode; CSI delegation may behave differently. It is not a promise
that existing data is unchanged. Refuse rollout if driver behavior is unknown, the
volume root is mismatched, or another consumer's ownership contract is incompatible.
Do not let a first workload mount silently perform a broad migration.

A supervisor must prepare a bounded, explicit path manifest of required updates,
record previous uid/gid/mode/ACL for each entry, and check symlinks/mount boundaries.
Review root-directory metadata separately. Prefer existing compatible group access;
otherwise change only approved ingestion-owned paths or migrate to a separately
prepared volume. Validate group-write on approved fixture paths and denied/preserved
unrelated paths first. No live data ownership command is part of this source change.
Rollout requires evidence that UID 1001 can create/update each intended artifact path
without granting access to unrelated data. Keep the path journal for reversal.

## Rollout and rollback: exactly five rendered consumers

Before deployment, save the previous immutable image digest and exact Git commit.
Using the maintained deployment templating function (`template_file` in
`.github/workflows/agent-context-deploy.yml`), render the five named consumer files
with the deployment's verified variables and save those rendered documents plus
checksums in the rollout receipt. Save a distinct new migration Job ID; existing
Jobs have immutable pod templates. Include the associated ScaledJob trigger document
only if unchanged and already managed by the same workflow.

Render the new reviewed commit in a separate clean checkout, using the tested image
promoted by immutable digest and an explicit new migration Job ID. Diff old/new
rendered documents before apply. Use the existing deployment workflow's ordering:
migration must complete successfully before worker/CronJob rollout. Apply only these
reviewed documents; never `kubectl apply -f manifests/`. Observe new jobs for all five
consumer paths, UID/security settings, expected write paths and product behavior.
Do not confuse declaration inspection with successful ingestion acceptance.

For rollback, pause new queue/cron launches through the maintained supervisor
procedure, use the saved previous rendered documents/image digest, and restore only
those five consumers. Do not `git checkout main`: main will contain this change.
Database rollback is a separate migration decision; do not blindly reverse schema
or rerun an old migration Job. Use a fresh, explicit Job ID if a reviewed migration
validation is required. Preserve in-flight jobs/data and record their disposition.
Reverse a bounded ownership change only from its reviewed per-path journal after
checking that subsequent writes would not be lost. Root image rollback alone is not
proof of data compatibility. Record final image IDs and outcomes for every consumer.

## Remaining S15 scope

Parser credential/filesystem/network containment, controlled dependency/fetch egress,
per-asset authority and minimized secrets, explicit supported SCIP configuration,
legacy-data disposition and live cross-tenant acceptance remain under #5614/#4720.
This image slice neither changes A09 callbacks nor closes S15. S21 scanner policy,
release pins and shared IAM remain unchanged; only the required personal_context
build-context staging entry is added for packaging consistency.
