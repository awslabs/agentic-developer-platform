# Maintained ARC controller security image

Upstream controller0.14.2 still ships vulnerable Go1.26.3, x/crypto0.49 and
x/net0.52. This recipe rebuilds its five original binaries with Go1.26.8 and
exact compatible module fixes (x/crypto0.56, x/net0.57, x/text0.41 and required
transitives). The source archive, Go toolchain and upstream runtime image are
immutable. The original distroless config, non-root user, entrypoint, trust
store and all five executables are preserved.

From this directory:

```sh
docker build -t arc-controller-security:candidate .
docker run --rm --network none arc-controller-security:candidate --help
```

Scan the exact output before publication. Publish to the target account's
`adp-arc-controller` ECR repository using a unique immutable tag, verify the
remote root digest, then supply `arc_controller_image=REGISTRY/REPOSITORY@sha256:DIGEST`
to factory Terraform if it differs from the recorded default candidate.
A new account must publish its artifact before applying the Helm release;
an empty repository is not a deployment-ready image.

Terraform upgrades the controller, org runner set and workflow runner pool
charts together to0.14.2 and overrides the controller image. The controller
uses its own image for listeners, so the rebuilt listener binary is included.
Existing custom runner images, GitHub secret references, IRSA settings,
replica bounds and resources remain configured separately.

## Validation and upgrade

See `docs/security/runs/2026-09-27/arc-controller-evidence.json` for exact scan
identities and checks. Unit packages cover GitHub requests, listener config,
metrics/scaling, API structs, glob matching, Vault and simulator behavior.
Controller integration tests run against checksum-verified Kubernetes1.35
`envtest` API server/etcd. Upstream's two static TLS fixture certificates had
expired: regenerate them using its `github/actions/testdata/generate.sh` before
running the suite. This changes only test credentials, never disables TLS
verification, and is not part of the shipped source archive/binaries.

Charts render with the pinned image, controller RBAC/service account, four CRDs,
and the runner-set secret reference. Terraform validates after building the
actual factory Lambda bundles with `build-agent-factory-lambdas.sh`.

**Existing ARC CRDs require a separate reviewed upgrade.** They live in the
chart's `crds/` directory; Helm does not update existing CRDs during upgrade.
Before applying Terraform, save existing CRDs/Helm values/revisions, inspect
the four0.14.2 CRDs from `helm show crds`, and apply the reviewed definitions
server-side without forcing ownership conflicts. Validate existing custom
resources against those schemas. Do not swap only the controller image under
an older chart. Check changes from0.13.1 through0.14.2, including custom TLS and
listener secret handling, against installation-specific values.

Live acceptance requires controller/listener readiness, a real authorized CI
job assigned to and completed by a runner, scale-down, and fresh actual imageID
collection (listeners as well as controller). No live rollout has occurred.
Rollback restores the reviewed chart/image and preserves compatible CRDs;
restoring the old controller reopens its Go vulnerability exposure.
