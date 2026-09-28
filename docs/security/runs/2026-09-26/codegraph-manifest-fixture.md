# CodeGraph manifest transition fixture — #6105

Follow-up to the image storage fix in #6388. Both the current and legacy CodeGraph
manifests now run the pre-baked CLI image with `sleep infinity`, `HOME=/data`,
UID/GID 1001, dropped capabilities and a read-only root. Runtime package installers
and the legacy install init container are removed. Writable `/tmp` and the existing
`/data` mounts remain explicit; the legacy `/workspace` and GitHub secret reference
are preserved. Readiness checks import the actual package.

`deploy-codegraph.sh` renders and validates all prerequisites before its first
Kubernetes operation. Its existing normal deployment mode remains available, and
`--render-only` performs no Kubernetes call. Both manifests are templates; the
legacy file must now be rendered rather than directly applied.

The operator must supply `CODEGRAPH_IMAGE=repository@sha256:<manifest-or-index-digest>`
and `CODEGRAPH_VALIDATION_RECEIPT` from the CodeGraph image gate. Rendering rejects
missing/incomplete/wrong-purpose receipts, malformed repository names and digest
mismatches. Only complete OCI `RepoDigests` entries establish the match: a Docker
configuration ID, including one placed bare in the repository-digest list, does
not. The receipt is a trusted operator-provided evidence artifact, **not a signed
or cryptographic approval**. A published image must preserve the tested OCI
manifest/index digest; a rewritten manifest/index requires new validation before
its receipt can be used. No image is published by these tools.

## Offline validation

Use Python 3.12+, local Docker, and PyYAML for the manifest gate. The image and
manifest gates must run against the same committed revision:

```sh
python3 modules/agent-context/scripts/validate-codegraph-image.py \
  --revision HEAD --output /tmp/codegraph-image
python3 modules/agent-context/scripts/validate-codegraph-manifests.py \
  --revision HEAD --image-receipt /tmp/codegraph-image/receipt.json \
  --output /tmp/codegraph-manifests
```

The manifest gate archives exact Git sources, verifies source/image labels and
actual repository identity, renders both variants, and starts each container with
its rendered command, literal environment, process security settings and writable
mount paths. It executes each rendered readiness/liveness probe, then the real
CodeGraph configuration/index/reopen/read/denial/deletion fixture. It uses fresh
tmpfs in place of PVCs; no Kubernetes secret, host credential, registry push or
production volume participates. Python `-O` is refused so identity/security checks
cannot silently disappear.

The isolated unit suite has 31 cases, including normal deployment with **every**
`kubectl` call stubbed, render-only with zero calls, preflight failure with zero
scale/delete calls, malformed references, incomplete/wrong-purpose evidence,
config-ID confusion, and optimized-validator rejection. Existing Agent Context CI
runs this suite. Actual Docker validation remains local because ARC lacks a Docker
socket (#4167) and hosted runners remain billing-blocked (#5362).

At source `c417f9c1796ce4464124c2d00900f85e12a57f61`, the actual image gate and both
rendered startup/probe/storage gates passed. The subsequent revision adds strict
reference validation and optimizer rejection and receives fresh final validation
in the PR evidence. All original scanner observations remain retained unchanged.

## Scanner evidence remains scoped

Independent original-version Checkov 3.2.346 scans (`--skip-download`) of the
rendered `c417f9c1796ce4464124c2d00900f85e12a57f61` variants reported:

| Variant | Rendered SHA-256 | Passed | Failed | Skipped / parse errors |
| --- | --- | ---: | ---: | --- |
| current | `3e1fa54ed83708ddc42075917858e2f0f43af6dfff082d9f5a30b1b085031c59` | 88 | 2 | 0 / 0 |
| legacy | `bc530c2cfd3236f3db6b9d7cb63ac1f29523e827326bbdf85af64f69a9d0c558` | 87 | 3 | 0 / 0 |

Remaining findings are CKV_K8S_40 (UID 1001) in both variants, CKV_K8S_35
(legacy environment-secret reference), and CKV2_K8S_6 in both. The latter is an
isolated-file NetworkPolicy association coverage limitation, not proof that the
deployed policy is absent. The focused changed-control run (CKV_K8S_22, 28, 37,
38 and 43) passed 10 checks with zero failures/skips. No suppression was added.
Original-version scanner JSON/logs are retained under
`/workspaces/projects/security25/fixture-sprint-6105-{current,legacy}-checkov-20260926.*`.

## Remaining acceptance and rollout hold

#6105 remains open across its 244 observations and 20 manifests. This change clears
the two CodeGraph manifests' local source/startup fixture gap only. Publication
with resolved immutable identity, approved canary/rollback, existing root-owned
PVC migration, actual fsGroup behavior and IRSA/secret delivery remain unverified.
`fsGroup: 1001` with `OnRootMismatch` can alter existing volume group ownership;
the isolated tmpfs fixture makes **no claim** about that rollout safety. Capture
and review affected PVC ownership before any deployment. Semantic code parsing,
other CodeGraph features, other image families and remaining controls also remain
outside this storage fixture. No shared gateway/tick or cluster deployment was run.
