# Shared release artifact interface

Security and DevOps use the same L5 wheel and worker archive. The protected
`l5-services-release.yml` workflow now accepts both services through `service`,
which defaults to `security` for the original Security flow. Publications to the
same environment are serialized, regardless of service. Worker lifecycle actions
share that concurrency group so rollout, rollback and publication cannot overlap.

The artifact phase is available to the combined-release owner. Security
publication is followed by a protected `L5 release and live qualification` job;
DevOps publication still awaits its separate driver integration. The Security
job checks the reviewed checkout and current engine authorization, verifies
owner-approved private fixtures for all four bounded selectors, and uses the
published immutable manifest and wheel digest to install and read back the
existing worker and repair Lambdas. It resumes the owned consumers before
running the setup, assessment, recovery and context selectors. A failed rollout
leaves the consumers paused, with durable SQL, queues and journals retained.

The Security job has an independent readback driver: after the selectors, it
fetches selected Product runs and results through the Product API, provider jobs
through the selected broker connection, and the installed Lambda code and queue
state under the exact deployment role. It retains those observations privately;
it does not turn them into passing acceptance records. The job currently stops
**before installation** because the evaluated delivery-evidence emitter still
lacks independent readers for the remaining scenarios, runtime revisions and
owned cleanup. Its offline, provenance-bound report can emit both accepted
schema shapes with explicit unresolved entries; it cannot promote a Product or
provider receipt, or a supplied success report, into passing flow evidence.
Selector logs and publication alone cannot satisfy the required qualification
artifact. The private fixture, credentials, journals and readbacks must be supplied
by the authorized release owner in the existing protected environment; this
workflow neither creates a test target nor authorizes provider spending on its
own. The remaining requirements in [the Security flow contract](flow/README.md)
still apply.

## Dispatch and authorization

| Input | Purpose |
|---|---|
| `service` | `security` or `devops`; selects the consumer, not another package build |
| `environment` | Existing protected `dev`, `test` or `prod` target |
| `adp_correlation` | Native operations invocation correlation |
| `adp_source_revision` | Immutable reviewed source selected by the engine |
| `adp_definition_revision` | Actual workflow definition revision |
| `adp_contract_digest` | Accepted version-2 delivery contract digest |
| `reviewed_source_sha` | Standalone publication source; optional for engine dispatch |

Supply all four `adp_*` inputs together for an engine dispatch. A standalone
publication supplies `reviewed_source_sha` and leaves all four empty. Conflicting
source inputs, an unreviewed branch, partial engine inputs or stale run identity
fail before publication. Both modes verify reviewed main ancestry.

Engine calls exchange the workflow's GitHub OIDC token with the existing
operations-release-job API. That API checks the current invocation, accepted
contract, source, workflow definition and roles. The check is repeated after the
build, immediately before publication. It does not create another execution
engine or a human approval step.

Use the existing `adp-gateway-deploy-<environment>` protected environment. Its
private configuration supplies `ADP_L5_RELEASE_BUCKET`,
`ADP_FOUNDATION_GATEWAY_URL`, `ADP_DEPLOY_REGION` and `ADP_DEPLOY_ROLE_ARN`.
`ADP_L5_RELEASE_ROLE_ARN`, when set, identifies the selected connection role;
otherwise the workflow uses `ADP_FOUNDATION_CONNECTION_ROLE_ARN`. The accepted
delivery contract must bind `job_mutation_role_arn` to the configured trusted
deployment role. The workflow cannot expand an already accepted contract. It
also verifies the actual AWS session before writing artifacts.

## Handoff to runtime qualification

The artifact phase writes these files beneath the private runner directory
`l5-services-release/`:

- `publication-context.json`: service, environment, source and definition
  revisions, repository/run/attempt identity, correlation and contract digest.
- `release-artifacts.json`: that same context plus the private bucket, immutable
  manifest version, wheel/archive keys, storage versions and SHA-256 digests.

The workflow retains these two records in the private `l5-release-artifacts`
evidence bundle using the platform's existing checksummed S3 evidence action.

`release-artifacts.py` reads each pinned storage version independently and
compares its bytes with the build. It also verifies that the worker archive
contains the exact selected wheel. Downstream stages must use the recorded
versions, rather than resolving a new latest object. The historical
`l5/security/<source>/<wheel-digest>` storage prefix is retained for compatible
rollback references; the bytes at that prefix contain the shared L5 package.

Worker archives use fixed ZIP metadata and omit installation caches, local wheel
URLs, bytecode and runner-specific console scripts. Package `RECORD` entries are
updated accordingly. Rebuilding the same pinned wheel in another checkout must
produce the same archive bytes, so a resumed publication can safely reuse its
existing immutable objects.

These records deliberately contain `phase: artifacts`. They contain no passing
live checks, cleanup outcome or acceptance criteria. A qualifying release must
retain the owner through the real service drivers and independent readbacks,
then publish the existing version-2 delivery and service qualification artifacts.
Private fixture data, credentials and raw observations stay outside the public
repository.
