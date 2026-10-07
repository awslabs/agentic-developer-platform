# Deployment evidence in S3

Gateway deploy, standalone frontend and migration workflows retain deployment
context and release JSON in the target account's private
`adp-<environment>-deployment-evidence-<account>` bucket. Container images remain
in ECR and frontend assets retain their existing publication path. These three
deployment workflows use S3 for context or release evidence. Browser and other
CI artifacts have separate storage contracts.

The app-owned Terraform module is `modules/gateway/infra/deployment-evidence`,
composed by `platform/automation-infra/deployment-evidence.tf` in the existing
trusted-automation state. Apply and inspect the saved infrastructure plan before
selecting the new workflow definition. The bucket is private, AES256 encrypted,
versioned, TLS-only, and retains current evidence for 30 days. Noncurrent
versions expire 30 days after becoming noncurrent. Destroy protection prevents
accidental bucket teardown. Writers have conditional-create access only to
`deployment-evidence/v1/`; the bucket policy rejects unconditional overwrites
and denies deletion by deployment publishers. The Gateway and its orchestration
tick Lambda have read access to that prefix, including version reads and
prefix-scoped listing. Neither gains Terraform-state access from this policy.

Each object key includes numeric repository ID, run ID, run attempt, kind and
workflow/component name. The envelope contains the exact evidence bytes and a
GitHub OIDC signature whose audience is `adp-deployment-evidence:sha256:<digest>`.
The reader verifies RS256 against GitHub's fixed issuer JWKS endpoint, the
content-specific audience, repository identity, run/attempt, and exact workflow
revision. Reusable workflows also require their callee identity. S3's own
publication timestamp must fall within the token's validity window. The token
is retained as a historical signature; it is never an AWS credential, and its
expiry does not invalidate evidence that was published while it was valid.
Unavailable historical signing keys fail verification closed.

The workflow stages the publisher and producers from `github.workflow_sha`, so
an explicitly selected older application revision does not need to contain the
new storage helper. Existing source and definition guards remain authoritative.
Each publish uses `If-None-Match: *`, requires a real S3 version ID, and prints
only the object location, version, and digest. A conflicting existing object
fails publication; retry with a new GitHub run attempt after diagnosis.

Consumers select `s3-oidc-v1` only when the approved workflow definition declares
`ADP_DEPLOYMENT_EVIDENCE_STORE`. They retain the genuine S3 version reference
instead of inventing a GitHub artifact ID. Workflow receipts and the final
runtime verifier compare this reference when re-observing a run. Context,
source, target, input, release digest and runtime-health checks are unchanged.
Historical definitions without the marker continue to read their GitHub
artifacts. Invalid or inaccessible S3 evidence never falls back to GitHub.

Rollout order: provision/review storage and IAM, ship the compatible reader in a
scoped Gateway release, then use the reviewed S3 workflow definition. An older
Gateway cannot consume new S3 receipts, so workflow success alone is not proof
of engine delivery acceptance. Re-running a historical failed workflow still
uses its historical definition; dispatch the new definition with the exact
reviewed application source instead. No historical artifacts need to be deleted
for the new transport to work.
