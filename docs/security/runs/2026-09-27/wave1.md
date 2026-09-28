# First live-image security remediation candidates

Refs #6492, #6505, #6506, #6499, #6511, #6512. The user authorized investigation and controlled deployment; deployed acceptance remains separate from this source change.

The controller and monitor are statically linked Go services with no subprocess calls. Their final images remove the unused apk/zlib chain. The controller also drops unused curl; the existing e2e diagnostic script uses BusyBox wget. Its Go dependency update to golang.org/x/net v0.56.0 fixes GO-2026-5942 / CVE-2026-46600, with required x/sys/x/term updates from Go module resolution.

Context MCP uses Python's verified HTTPS client to fetch the same RDS trust bundle and probe the same health endpoint. Its served code does not invoke curl; omitting curl also omits libcurl and its open Critical matches. Required TLS validation and nonzero health-check failure behavior are preserved.

`wave1-candidate-evidence.json` binds before/after raw scans to exact config and archive hashes. Controller and monitor candidates have zero Critical/High matches; Context MCP has zero Critical and 50 High raw matches remaining. No new Critical/High package/advisory pair appears against the live baseline. Lower-severity data is retained in raw artifacts but was not triaged. A source fix or candidate scan is not live clearance.

Validation:

- Controller and monitor Go package tests; controller tests are repeated after the dependency update.
- `pytest -q modules/domain-apps/superplane/tests/test_controller_runtime_surface.py` passes all nine source-surface and non-root guards.
- `python3 modules/domain-apps/superplane/src/superplane-platform-monitor/tests/verify_container.py <candidate-image>` exercises the packaged monitor with synthetic loopback responses, including health rejection/recovery, UID, trust-store/timezone retention, and no credential logging.
- Context MCP runs with network disabled and a read-only root: `/health` returns 200, missing ACL configuration keeps `/ready` at 503, its actual image HEALTHCHECK command succeeds, and directing that command to an unused port fails. The curl binary is absent.
- Syft catalogs each saved image archive; Grype scans that exact SBOM with no suppression or only-fixed filtering. Syft's config identity equals the archived config SHA-256; Docker's root OCI index is recorded separately.

Private/local scan artifacts and detailed run logs remain under `/workspaces/projects/security27/`. The AWS dev-box identity cannot patch deployments; the existing deployment role is not assumable by that identity and the CodeBuild preflight cannot reach the EKS endpoint. No permission or network boundary was widened. Live rollout awaits a usable authorized deployment identity/path.
