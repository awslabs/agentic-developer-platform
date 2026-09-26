# Executor kubectl dependency repair — #6124

The active executor image downloads Kubernetes v1.31.4 kubectl, checksum
`298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f`.
A fresh unsuppressed scan of those exact upstream bytes found 50 observations:
2 Critical, 23 High, 24 Medium, and 1 Low. They belong to Go 1.22.9 (43), x/net
0.26.0 (4), x/text 0.16.0 (1), gorilla/websocket 1.5.0 (1), and moby/spdystream
0.4.0 (1). These fresh counts are not the frozen Superplane family denominator.

The Dockerfile now builds the same v1.31.4 command source using pinned Go 1.26.8
and an exact reviewed module patch. Security dependencies are x/net 0.56.0,
x/text 0.39.0, gorilla/websocket 1.5.3 and moby/spdystream 0.5.1; the patch also
retains the necessary Go module selection changes. Source archive checksum and
commit are fixed. The compilation phase has no network, uses readonly module
resolution, and never performs a floating dependency update. Runtime retains
upstream license notices and the downstream build recipe.

The binary explicitly reports `v1.31.4+adp.security.1`, Go 1.26.8, source commit
`a78aa47129b8539636eb86a9d00e31b2720fe06b`, and a modified source tree. It is
not presented as an unmodified upstream release. The workspace interface still
accepts Kubernetes 1.31–1.35. A single replacement 1.35 client would newly lose
supported skew for 1.31/1.32, so this patch preserves current command semantics.
Existing skew for newer targets and upstream support for 1.31 remain unresolved;
a separate client-selection design is required. Terraform's exact 1.9.8 saved-plan
contract, AWS CLI version, and reviewed Python image input remain unchanged.

Validation:

- Isolated nonroot static compilation with Go 1.26.8 and no network passed.
- 537 upstream tests/subtests pass across create/apply/config/auth commands and
  client-go remotecommand transports; the selected SPDY transport package has no
  test files. Tests ran with networking disabled (loopback fixtures remain usable).
- The initial upstream test run had 535 passes and two apply-set golden failures.
  GitHub's source archive substitutes the export placeholder in default version
  metadata; both goldens expect the literal `v0.0.0-master+$Format:%H$`. Re-running
  with that test-only linker value passes all 537. The production build uses its
  truthful downstream version instead. The original failures are retained in
  `followon-superplane-kubectl-original-test-failures.json`.
- 93 existing workspace bootstrap adapter/permission tests and 22 executor build
  boundary tests pass using the required isolated CLI stores.
- The exact final binary passes nonroot/read-only/offline version reporting,
  isolated kubeconfig/context operations, client dry-run namespace/configmap/
  deployment creation, and kustomize rendering. No provider/cluster calls occur.
- Binary SHA-256:
  `b3aa6322e5da4e85f06ceb3a7c99555b21f0f709b103891109b9360de2796ba7`.
- The same frozen raw Grype DB (built 2026-09-25T06:31:49Z, no ignores or updates)
  reports zero matches for this binary. Baseline and final raw reports are retained
  beside this note. Go's root module metadata is `(devel)` for a local source build;
  scanner absence is not acceptance of every Kubernetes advisory or proof of
  runtime applicability. The root source/version remains explicitly identified.

This repairs one executor tool. It does not close #6124, accept remaining
Terraform/AWS CLI/Python/OS observations, establish full image clearance, or
change any running workload. No publication or rollout occurred. Original
Superplane image/config identities and all 2,597 frozen occurrences remain intact.

The production Dockerfile's `kubectl-builder` target also builds successfully,
including archive checksum validation, clean-source patch application, readonly
module dependency resolution, and network-disabled compilation. Its output has
exactly the same SHA-256 as the separately tested/scanned binary above. Nonroot,
read-only startup in the built stage reports the same downstream version.
Builder-only local image index: `sha256:4c2cc378019b70520fae441b6b167881a6609581e58459d61f3c18442a8f2090`; this is not a runtime image or a
published digest. The Python base argument supplied for parsing later stages was
not consumed by this builder-only target. The full executor image remains a
separate acceptance item. Existing Docker warnings for an unset default
`PYTHON_IMAGE` preserve the mandatory reviewed-input contract.

Full executor image acceptance was then completed locally from implementation
commit `64c27e318` (default `paid-worker` target) and the explicit Python 3.12 base
input recorded in `followon-superplane-executor-kubectl-image-acceptance.json`.
The scanner config ID, independently verified against Docker-save config bytes,
is `sha256:859b444d4862efc8a2c9e5b4533926b3fad011b7a33c521366a0a37780fa3430`.
The exact image retains 307 native observations: 8 Critical, 101 High,
129 Medium, 23 Low and 46 Negligible. All eight Critical observations are in
`/usr/local/bin/terraform`; fixing that version-constrained tool is the next
concrete repair, not an implied acceptance. Python, AWS CLI and OS residuals
remain owned by #6124. The complete raw unfiltered report is retained losslessly
as `followon-superplane-executor-kubectl-image-grype.json.gz`; the receipt records
both uncompressed and compressed SHA-256 values.

The installed runtime passed pip consistency and package/data imports, exact
kubectl hash and offline creation checks, and unchanged Terraform/AWS CLI version
checks. Without authority, the actual paid-worker entry point refuses startup;
the service remains idle and never serves assignments. These checks ran with
networking disabled, a read-only root filesystem, dropped capabilities, default
UID 65531, and temporary configuration stores. The reproducible smoke is retained
as `followon-superplane-executor-runtime-smoke.py`. No image was published and no
workload was changed. Full family/advisory reconciliation remains incomplete.
