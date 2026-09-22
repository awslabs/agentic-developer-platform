# Cyber job authority and runtime isolation (#5616)

The GitHub workflow registers triage and static jobs through
`/internal/v1/agent/arc/cyber/jobs`. The gateway verifies the registered workflow's
GitHub OIDC signature and a request-bound STS proof. It resolves the verified
human, tenant, Cognito upload identity and current team membership from protected
records. Body fields and repository variables cannot select an owner.

The gateway only accepts canonical chat uploads belonging to that owner. It
requires S3 versioning, hashes the exact version, records script bytes and their
digest, rechecks caller authority, then enqueues. Queue resource policies deny
every producer except the gateway role. A shared SQS body is therefore delivery
from a trusted supervisor, rather than an authentication claim from a caller.

Workers have no IAM read grant on tenant objects and cannot assume a role to mint
one. A job contains a 15-minute GET capability for one object version, its size
and digest. The staging supervisor rejects redirects, foreign destinations,
wrong versions, expired or oversized downloads, and changed content. Scripts
are registered inline bytes; `script_s3_uri` dispatch is refused. Results use a
server-derived workflow/run/attempt namespace and are read through the broker.
Private human uploads are not granted to a service workflow merely because a
human registered that service.

Triage's native parsers, static Mode A, and Mode B scripts all execute in a fresh
interpreter after Linux Landlock and seccomp are installed. Landlock permits
immutable interpreter/tools/rules, exact staged input files, and private scratch.
It denies projected credentials, `/proc`, other jobs and writes to application
code. Seccomp blocks networking, io_uring, process inspection, namespace/mount
operations, and process-group escape. Restrictions survive subprocess execution.
The supervisor closes inherited descriptors, clears the environment, caps output
and resources, and kills the whole process group even after successful analysis.
An unsupported kernel or missing isolation dependency produces a failed stage;
there is no AST-only fallback. AST validation is an additional compatibility check.

The trusted queue/download supervisor retains SQS and result-write authority; it
never passes those credentials or network access to sample-processing code. This
is the trust boundary: a malicious script or compromised native parser cannot
act as that supervisor. The CAPE host's unused tenant-bucket read grant is also
removed; it receives samples as uploads in the existing dynamic-analysis path.

## Rollout order

This PR changes code and configuration only. Follow the canonical deployment
guide for a separately authorized rollout. Preserve the existing engine transport
and keep scans on demand.

1. Confirm the malware workflow's existing `ADP_ARC_MODEL_BINDINGS` entry pins its
   exact repository ID, workflow ref, runner role and malware persona. Verify the
   human has a verified GitHub identity, Cognito upload identity and current team
   membership. Confirm the chat sample bucket has versioning enabled.
2. Apply Cyber broker IAM and broker-only task queue policies; deploy the gateway
   code and rendered Cyber configuration. Old direct-queue producers are refused.
   Do not drain old unsigned messages into the new execution path; let them fail
   registration or remove them through the separately authorized operational flow.
3. Deploy the authenticated workflow/client and rebuilt worker image together.
   Worker rollout removes ambient sample/script S3 access. The standard gateway
   ConfigMap renderer supplies the account, bucket, queues and results table.
4. Run the exact-image `security-test` Docker target under production container
   restrictions and a registered harmless sample through triage, static Mode A
   and a legitimate Mode B script. Require Linux Landlock ABI 3+ and libseccomp.
   Verify wrong-owner, tampered, unregistered and expired jobs fail explicitly.

Broker tests use real RSA signatures, SQL identity resolution and versioned Moto
S3/SQS. Linux kernel probes exercise allowed analysis and filesystem, network,
process, timeout and output restrictions without mocks. They do not claim that
the live cluster has received or validated this rollout.
