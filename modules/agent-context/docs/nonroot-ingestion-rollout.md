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

## Actual Mountpoint acceptance and rollout

The deployed platform-data PV uses S3 Mountpoint CSI v1.15 systemd, not a POSIX
volume. The local ownership fixture above does not establish live compatibility.
See [Mountpoint ownership acceptance](mountpoint-nonroot-acceptance.md) for the
current driver evidence, explicit FUSE UID/GID/modes, supplementary read group,
least-privilege isolated probe and bounded production rollout/rollback plan.

Do not chmod/chown S3 objects or rely on fsGroupChangePolicy to set this driver's
ownership. The source PV grants UID1001 owner-write and GID1001 read/traverse;
Zoekt is an additional read-only consumer and must be included in acceptance.

Snapshot each actual controller and immutable rollback image before changing it.
Render only the reviewed five ingestion consumers plus the separately reviewed
PV/reader changes; do not bulk-apply manifests. Absent synthesis and expired or
absent migration Jobs require explicit lifecycle decisions. A filesystem change
does not itself authorize rerunning a migration or creating a missing consumer.
The old vuln-scan tag must be replaced with a verified rollback artifact before
that consumer changes. Existing Pods may retain old FUSE options until replaced.

The promoted PR6043 image can run the isolated synthetic probe, but it predates
PR6060 scratch/retry fixes. Production rollout requires a newly tested immutable
candidate incorporating the reviewed fixes. Never use the broad build/deploy
workflow as a validation shortcut because it updates latest and deploys.

On canary failure, stop new attempts and restore only the recorded changed fields
with current UID/resourceVersion guards, including old mount options and pullable
images. Verify newly mounted rollback Pods before resuming controllers. No data
ownership reversal or database rollback is implied. Preserve in-flight jobs and
record their disposition; never remove production PV finalizers or force-unmount
shared node paths. Source templates alone are not live acceptance.

## Remaining S15 scope

Parser credential/filesystem/network containment, controlled dependency/fetch egress,
per-asset authority and minimized secrets, explicit supported SCIP configuration,
legacy-data disposition and live cross-tenant acceptance remain under #5614/#4720.
This image slice neither changes A09 callbacks nor closes S15. S21 scanner policy,
release pins and shared IAM remain unchanged; only the required personal_context
build-context staging entry is added for packaging consistency.
