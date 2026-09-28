# CodeGraph writable-home fixture — #6105

The pre-baked CodeGraph image created a system user whose home was `/nonexistent`.
CodeGraphContext 0.6.13 uses `Path.home()` for configuration and embedded database
storage; the legacy manifests' `CGC_HOME` variable does not redirect those paths.
Consequently, an import/`--help` check passed while `cgc config set` failed with
`Read-only file system: '/nonexistent'` under a restricted runtime.

The image now sets `HOME=/data` and creates that directory owned by UID/GID 1001.
A read-only runtime must mount `/data` writable by this user. This change does
not render, apply, replace or migrate either legacy CodeGraph deployment.

Run against a committed revision (Docker on the local Unix socket, Python 3.12+):

```sh
python3 modules/agent-context/scripts/validate-codegraph-image.py \
  --revision HEAD --output /tmp/codegraph-runtime-evidence
```

The runner builds only archived Git sources with a clean Docker configuration,
records the exact source archive SHA-256 and image ID, and runs without network,
host credentials, production volumes or Kubernetes access. Its two containers
use UID/GID 1001, a read-only root, zero capabilities, no-new-privileges and the
runtime's default seccomp filter. The positive fixture provides disposable `/tmp`
and `/data` tmpfs mounts. The negative fixture omits `/data`.

Acceptance proven by the fixture:

- Package/CLI identity, process security settings and denial of writes to the
  installed package, CLI executable, `/etc/passwd` and root filesystem.
- Configuration persists under `/data/.codegraphcontext`; the actual indexer
  inserts a disposable Markdown repository and file into KuzuDB. Separate CLI
  processes reopen and query the stored records.
- The query surface rejects mutation, and repository deletion is denied by
  default. Only this disposable fixture enables deletion, executes it, and
  verifies that the repository count becomes zero.
- Removing the writable data mount produces an explicit filesystem denial.

Local execution at `12666b3e220232a091406e7266798d148a341727` passed both containers;
source archive SHA-256 `25ee648d2add038540a025bdb143910cff8236df5bced9038ef69314bed3cee3`,
image ID `sha256:a642f48d3d349a93f06103fd8f44700357b7400c822d58bd5cb3c54f9e92c215`.
Run the gate locally for each reviewed revision and retain its receipt. The
attempted PR Docker gate failed before building because `arc-runner-org` has no
`/var/run/docker.sock` (run 36228525539). This is the open #4167 infrastructure
limitation; GitHub-hosted runners are also blocked by #5362. No automated Docker
check is claimed, and existing repository CI checks remain unchanged.

Final worker gate at `6558bbdbc8ce055cfb0cd4329cdd152d0ecdd479` passed with source
archive `396eb52947f0c237ac1eab8dbb2a513bfd635a6421d84472c65ec5780abd22f7`
and image `sha256:570b7872454312959f14bbd1e97c0d1bed7128aeb94156121788384d83820b51`.
The independent reviewer reruns the exact revision and retains a separate receipt.
An earlier concurrent same-tag review failed its negative container (Docker 125,
image unavailable); it remains incomplete. Per-invocation image tags and explicit
source-label validation now prevent that build/inspect race.

## Remaining acceptance

#6105 remains open. This is one image's local filesystem/storage fixture, not
completion of its 244 observations across 20 manifests. Both CodeGraph manifests
still install dependencies at startup and need a reviewed transition to the
pre-baked image, resolved deployment image digest, writable-volume ownership and
canary/rollback validation. Legacy persisted data migration and IRSA/live workload
acceptance are unproven. Semantic code parsing (including offline parser assets),
other CodeGraph features and other image families are outside this storage
fixture; a Markdown graph does not establish those behaviors. No deployment or
original scanner-selector disposition was changed.
