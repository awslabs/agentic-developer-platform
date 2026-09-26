# Superplane controller tracked dependencies — #6124

The maintained controller module required `golang.org/x/net v0.33.0` and
`golang.org/x/oauth2 v0.23.0`. Both its Dockerfile and the declared release build
consume this tracked module graph directly. Updating the Go toolchain alone did
not repair these library versions.

This repair pins `x/net v0.55.0` and `x/oauth2 v0.27.0`. Go minimal version
selection also updates `x/sync v0.21.0`, `x/sys v0.45.0`, `x/term v0.43.0`, and
`x/text v0.37.0`. An unsuppressed binary scan then identified
GO-2026-5970 / CVE-2026-56852 (native High, CVSS 3.1 7.5) in that x/text
version; x/text is explicitly raised to its patched version `v0.39.0`.
`go mod tidy` preserves the existing Kubernetes and
controller-runtime versions. The minimum Go version rises from 1.23.0 to 1.25.0
because patched x/net requires it; the existing selected toolchain remains
Go 1.26.8. The two controller CI toolchain installations now request 1.26.8
explicitly, including the shared transferred-component test job.

Fresh GitHub Dependabot observations on 2026-09-26:

| Alert | Advisory | Tracked package | First patched version |
| --- | --- | --- | --- |
| 273 | CVE-2025-22870 / GHSA-qxp5-gwg8-xv66 | x/net | 0.36.0 |
| 274 | CVE-2025-22872 / GHSA-vvgc-356p-c3xw | x/net | 0.38.0 |
| 275 | CVE-2025-22868 / GHSA-6v2p-p543-phr9 | x/oauth2 | 0.27.0 |
| 276 | CVE-2026-25680 / GHSA-5cv4-jp36-h3mw | x/net | 0.55.0 |

These are dependency-version remediation claims, not assertions that every
advisory is reachable in the controller. GitHub alert acceptance must be checked
after merge. This does not close the six-image Superplane family issue, reconcile
all 2,597 frozen observations, or prove a deployed image changed. Original
scanner selectors and artifact identities remain unchanged.

Validation uses the existing Go 1.26.8 builder pinned to
`public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65`,
a dedicated module/build cache, no host configuration or credentials, and disabled
container networking after dependency acquisition. No live cluster e2e or
integration build tags are invoked. Existing unit tests cover fake Kubernetes
reconciliation, local HTTP transports, service-token redirect prevention,
management registration/revocation, and execution behavior.

Validation results:

- `go mod verify`: all downloaded modules verified.
- `go vet -p 2 ./...`: passed.
- `go test -p 2 -count=1 -json ./...`: 316 passing tests/subtests; no failing or
  skipped tests (main and api/v1 have no test files).
- Existing source/build declaration and controller runtime-surface Python checks:
  48 passed, through the required isolated CLI-store wrapper.

- Final static binary build (`CGO_ENABLED=0 go build -p 2 -mod=readonly
  -trimpath`) and isolated `--installation-preflight`: passed. Preflight reports
  `governed_provisioning=false` and `workspace_ready=false`, as expected without
  configured runtime authority.
- Final binary SHA-256:
  `0d76c218db4932dd37325bf58a3e328c38739fb566e0ada6c4fadf9026b98b1e`.
- Frozen Grype DB built `2026-09-25T06:31:49Z`, schema `v6.1.9`, with auto-update
  disabled and `ignore: []`: zero findings for this exact binary after the x/text
  repair. This is a binary scan, not a full container scan or live acceptance.

Raw final scan is retained beside this note as
`followon-superplane-controller-binary-grype.json`. The initial candidate scan
retains the exact High x/text observation for candidate SHA-256
`73ebb485ac4a6ae74315171f04fd27b2158f0f6364e86452576ba3c6c0286357` in
`followon-superplane-controller-initial-binary-grype.json`. Both paths refer to
local compiled file targets, not Docker config IDs or OCI manifests.

Full local image follow-through uses implementation commit `a374051aa` and the
unchanged production Dockerfile. Its separate Alpine-built binary SHA-256 is
`1f5ebd0a1bb34777a78fc47cded4027e0479444d9bf0ba8c46f8c2729fe4e8fb`.
The scanner config ID, independently verified from `docker save` config bytes, is
`sha256:aa90eed839abc21f8b29d4a599f3b23e30110d2a135cb2892c7259446d9d1f13`.
Offline, read-only startup passed as UID/GID 65532 with all capabilities dropped.
The exact full image retains five native findings, owned by #6124:

| Package | Advisory | Native severity |
| --- | --- | --- |
| zlib 1.3.2-r0 | CVE-2026-85091 | High |
| nghttp2-libs 1.69.0-r0 | CVE-2026-58055 | Medium |
| busybox 1.37.0-r31 | CVE-2025-60876 | Medium |
| busybox-binsh 1.37.0-r31 | CVE-2025-60876 | Medium |
| ssl_client 1.37.0-r31 | CVE-2025-60876 | Medium |

The frozen database supplies no fixed-version candidate for these observations;
this is not risk acceptance. Raw scan and identity/startup receipt are retained
in `followon-superplane-controller-image-grype.json` and
`followon-superplane-controller-image-acceptance.json`. The original 39 controller
occurrences remain historical evidence, not a directly comparable denominator
for this fresh raw scan. This completes local image build/startup/rescan evidence,
not full family acceptance or deployment. No image was published.
