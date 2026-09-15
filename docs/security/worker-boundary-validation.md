# Protected-worker boundary validation

The credential runtime is merged in #5196. The IAM definitions can merge while
the checked-in `.github/deployment-holds/webhook-infra.md` hold prevents the
webhook workflow from applying infrastructure. #5176 prerequisites and #5195
migration remain open. No live IAM or activation flags changed.

The hold covers this merge, later infrastructure changes, mixed code/infra
releases and manual webhook workflow dispatches before packaging or state
mutation. Code-only pushes can continue deploying Lambda code. The workflow
summary reports held releases explicitly. Remove the hold only in a reviewed
rollout change after the documented prerequisites are satisfied; it is not a
substitute for a fresh scoped plan or live acceptance.

The policy is rendered from the actual Terraform locals by
`modules/agent-factory/webhook-ingress/tests/render_worker_boundary.py`, replacing
provider outputs with fixture resource ARNs. The rendered compact boundary is
5,070 bytes, below the IAM managed-policy limit. This renderer is a test helper,
not a deployment policy generator or a Terraform plan.

Validation completed:

- Terraform initialize without backend and `terraform validate`: passed, with
  existing provider deprecation warnings.
- 42 boundary/control/ScaledJob manifest tests: passed, including evaluation of
  actual Terraform expressions without AWS or Kubernetes state.
- Deployment eligibility tests exercise real Git histories, held infrastructure
  and mixed releases, manual dispatch, code-only releases, and failure handling.
  The worker safety CI job runs these alongside the boundary/manifest tests.
- Read-only AWS `SimulateCustomPolicy`, account `879318057152`: 16 expected
  decisions with an Allow-all identity policy intersected by the new boundary.
  Denied self IAM mutation, tenant/marker secret reads, direct STS, Bedrock,
  Cognito admin, authoritative DynamoDB writes, unrelated gateway routes,
  foreign-environment queue/artifact access and direct KMS. Allowed the scoped
  broker, own queue/artifacts, advisory correlation writes and provenance metric.
- CloudWatch `logs:PutLogEvents` on a concrete log-stream ARN: implicitDeny.
  A standalone policy allowing that action with Resource `*`, an exact stream
  ARN or a stream wildcard also returned implicitDeny, without missing context.
  Keep a real scoped logging canary as a release gate; do not infer a working or
  broken live grant from this simulator result or widen permissions to satisfy it.

A fresh live plan and provider canary are still required. Shared marker signing
and Door access need authenticated mediation, queue/artifact housekeeping needs
supervisor isolation, and the old role's Kubernetes access and administrator
attachment need the verified migration in #5195. The deny-all-secrets boundary
must not be weakened to preserve those shared-key paths.
