# Build-lock dependency repair — 2026-09-27

GCP authentication-plugin build locks update gRPC1.72.2→1.83.2 and OpenTelemetry SDK1.41.0→1.44.0, closing five source alerts. Trivy locks update moby/go-archive0.2.1→0.3.0, closing one source alert. These are actual build locks retained by both recipes.

The GCP upgrade also updates linked oauth2, so its actual plugin was rebuilt and its package tests passed. Trivy was rebuilt with Go1.26.8, CGO_ENABLED=0, GOEXPERIMENT=jsonv2 and its original flags; the resulting SHA256 is byte-identical to the published binary727fbbbbdbd227f396f384e536cbeeac359b4c01c4db880c4871849188ca3d81. Its production dependency graph does not link go-archive or legacy Docker.

Two legacy Docker source advisories have no fixed version for that module path. The attached applicability review independently inspects both production and all package test dependency graphs. Affected daemon/archive/container-router packages and their functions are absent from both. The test graph uses generic HTTP/router/type helpers through aquasecurity/testdocker. This evidence supports reviewer consideration of a not-used disposition; this PR does not dismiss alerts or claim the legacy module is patched. Actual build locks and tests remain present.

Reproduce with the exact pinned Trivy source plus these locks: `CGO_ENABLED=0 GOEXPERIMENT=jsonv2 go list -mod=readonly -deps ./cmd/trivy` and `CGO_ENABLED=0 GOEXPERIMENT=jsonv2 go list -mod=readonly -deps -test ./...`. Full graphs, advisory records and source-file hashes are included. No exploit was executed.
