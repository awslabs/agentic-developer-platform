# Worker toolchain security rebuild

The agent runtime (`agent-worker-image/Dockerfile`) rebuild preserves Terraform
1.15.7 and AWS CLI2.37.4 while fixing their embedded dependencies. Terraform uses
Go1.26.8 with a locked compatible module patch, including x/crypto0.56,
x/net0.58, x/mod0.40 and gRPC1.83.1. AWS CLI reuses the merged source-build recipe
with CPython3.14.7 and upstream dependency hashes. It is an ADP-built portable
executable, not the AWS-signed ZIP. Provenance/licenses are installed with it.

Node24.21.0 is now installed from a checksum-pinned official release, replacing
the unpinned NodeSource setup script and package source. The upstream Node version
is unchanged; scanner comparisons therefore include a provenance/matching change
for Debian Node-source findings. npm11.20.0 replaces the old bundled dependency
graph. Corepack's prepared pnpm12.6/Yarn4.18 cache is retained for the image user.

The candidate has **24 Critical / 106 High raw matches**, versus25/149 in the
verified live image, with no new C/H advisory-package pairs. Remaining curl/OS
findings stay open. Exact scanner/config/publication receipts are included.

Validation: old Terraform saved plan applied by rebuilt binary and rebuilt plan
applied by old binary, both1.15.7, with state/output roundtrip; 12 offline AWS CLI
service-model checks and invalid-command rejection; actual packaged Task API
useful/cancelled/malformed scenarios with no network requests; shared-contract
selfcheck; nonroot node/npm/pnpm/yarn/Codex/AWS/Terraform version checks. All use
isolated BG_CONFIG_DIR and no real cloud credentials. Corepack tests read the
baked cache explicitly while HOME is isolated. Terraform tests cover local
built-in resources; cloud provider/auth/provisioning acceptance remains live work.

Unique ECR publication is verified by digest. No deployment occurred. Update the
worker and prepull templates coherently through the authorized workflow, retain
previous imageIDs, run a real authorized job and cancellation/credential-expiry
checks, then collect actual imageIDs and rescan before closing assigned findings.
Rollback restores the previous image and its exposure.
