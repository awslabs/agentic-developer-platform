# Worker tool security overlay — 2026-09-27

Candidate `adp-agent-runtime@sha256:cfbe0b18dcc3e055738f52ef1004624aefe62cc165bef444b7edc933992ef89a` adds repaired Terraform 1.15.7 (Go 1.26.8, gRPC 1.83.2) and AWS CLI Python 3.14.7 adapters statically linked with Expat 2.8.5 to the exact current worker `5fe1725…`. Source recipe: `modules/agent-factory/agent-worker-image/Dockerfile.high-tools-overlay`. Build source commit `317f710b5f1ab3b7241e698f1f5f364ce282192e` was subsequently rebased without recipe changes.

All 22,762 `/app` file hashes match the base; the complete Docker Config and all original layers are preserved. `verify-high-tools-overlay.py` reproduces this comparison. Private complete hash inventory and raw archives remain in `/workspaces/projects/security27-high-tools`.

Acceptance passed: actual AWS CLI local valid/malformed XML, offline service models and invalid-command rejection, shared-contract selfcheck, real Terraform null-provider plan/apply, and artifact binary hash verification. The baseline worker and fixed Terraform passed saved-plan compatibility in both directions; the candidate also passed the same provider roundtrip. No cloud resource was provisioned. The prior worker useful/cancelled/malformed task fixture was unavailable; that scenario suite was not rerun, and task-code preservation is proven by hashes instead.

Frozen scan: **24 Critical / 105 High native matches**. Revalidated exact curl binaries and Python tarfile source bind 55 prior dispositions to this candidate: **0 Critical / 74 High remain after these tool reviews**. All remaining matches are OS packages, including the exact zlib vendor-unaffected match handled separately by the closure reconciler. This tools overlay is not full worker High closure and is not a live deployment claim. Expat adapter replacement repairs an embedded library not represented by the raw scanner count.

`worker-overlay-tool-review.json` binds native match indices and scan hash to actual candidate bytes; no package-name-only inheritance was used. `scan-receipt.json` preserves raw counts and archive/SBOM/Grype hashes. The full frozen scan is `worker-overlay-scan` in the private evidence directory.
