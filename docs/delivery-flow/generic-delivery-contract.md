# Product-owned delivery contracts

An operations story can opt into `operational_release_binding.delivery_contract`
version 2. The accepted plan stores this configuration snapshot and its digest.
Repository, predecessor, execution policy, budget, concurrency and credential
bindings remain unchanged. No additional human gate is added.

The agent triggers the configured GitHub Actions workflow. The workflow deploys
and runs the product's API, CLI or UI checks. The engine verifies run identity,
evidence integrity, required check outcomes and cleanup. Application assertions
and deployment commands belong in the workflow.

## Configuration

This fictional example belongs inside an operations story's release binding:

```yaml
repository: example/payments
component: payments-api
target: payments-test
workflows: [ship-payments.yml]
default_branch: main
predecessors:
  - address: payments/checkout/build/implement
    receipt_kind: verified_merge
build_prerequisites: [payments/checkout/build/implement]
delivery_contract:
  schema_version: 2
  connection_id: fictional-connection
  resource_kind: service
  resource_id: payments-test/api
  baseline_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  definition_revision: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  workflow_inputs:
    environment: test
    suite: checkout
  required_checks: [api-smoke, cli-roundtrip]
  cleanup_required: true
  evidence_artifact: delivery-evidence
```

Replace illustrative revisions and connection identifiers before acceptance.
The connection must be permitted by the flow policy. Configuration is not a
secret store; workflows use their usual secret and environment facilities.
Another product can name different components, workflows and checks without an
engine release.

Workflow inputs are product-defined: an `environment` input is optional, and
names such as `stage` have no special meaning in version 2. Git branch names
such as `release/stable` are supported. The contract allows 32 business inputs;
the context envelope also accommodates the four engine transport fields.

The current provider resolves account and region through a registered AWS role.
The accepted configuration supplies the resource boundary; the workflow checks
its deployed state. Version 2 consumes GitHub Actions release artifacts, even
when the separately authenticated dispatch context uses S3. A missing or invalid
GitHub release artifact never falls back to S3. Legacy contracts retain their
existing S3 release evidence support.

## Workflow interface

Declare inputs `adp_correlation`, `adp_source_revision`,
`adp_definition_revision` and `adp_contract_digest`. The engine supplies these
transport values and the exact accepted business inputs. Workers cannot override
them. The existing correlation run-name and version-1 context artifact protocol
continue to apply.

Publish one artifact named `<evidence_artifact>-<github.run_attempt>` containing
only `delivery.json`. The envelope contains:

- `schema_version: 2`, `contract_digest`, `correlation`;
- `repository_id`, `run_id`, `run_attempt`, `workflow_path`, `workflow_revision`;
- `source_revision`, independently observed `actual_revision`;
- `account_id`, `region`, `resource_kind`, `resource_id`, `component`;
- `checks`: unique objects with `id`, `outcome` (`passed`, `failed`, `skipped`)
  and `evidence_ref`;
- `cleanup`: `passed`, `failed` or `not_required`;
- `produced_at`: timestamp with a timezone.

The workflow owns application-specific evidence and schema validation. Each
check references that evidence. Workflow success alone cannot complete the node.
The engine authenticates the artifact digest, matches the envelope to the
correlated run and configuration, and requires all configured checks to pass.
Missing, failed or skipped required checks, failed required cleanup, mismatched source or
attempt, and expired evidence prevent acceptance.
Additional checks are informational; the receipt records the configured required
checks, and the original artifact retains all results.

## Optional workflow job authorization

A workflow that checks current delivery ownership before its effects can set
`job_mutation_role_arn` in the accepted version-2 contract. This pins the workflow's
AWS mutation role separately from the registered connection role. Omitting it
preserves existing contract serialization and digests.

The existing `/orchestration/operations-release-job/check` endpoint accepts a
GitHub OIDC token for audience `adp-operations-release-job-v1` and the usual job
identity fields plus `contract_digest`. The server requires the exact accepted
digest, roles, source and workflow, the app-dispatched provider run, current
execution/claim, and the held physical-target lease. A changed owner, paused
flow, stale run or released target is refused. No application or cluster probe
is added to the engine.

The sibling `publication-intent` and `publication-observed` endpoints accept
the same identity plus `image_digest`. They retain an authenticated, idempotent
same-digest journal. The workflow must independently read back the immutable
publication before reporting it observed. These acknowledgements neither mark
deployment successful nor release the target. Only the ordinary verified
workflow outcome, required checks and cleanup can do that. A retry requires the
existing authenticated terminal-run reconciliation and a fresh reservation.
Legacy grouped jobs continue to use their original guard and publication path.

## Recovery and compatibility

A target lease serializes deployments to the same physical resource. A durable
request prevents a repeated agent call from dispatching the same operation twice.
Uncertain dispatches require reconciliation. Repeated successful observation
reuses the verified receipt. Existing retry allowances, budgets and claim fences
remain in force.

A terminal failed, cancelled or timed-out workflow with authenticated context releases its own target
reservation and records the failure for the flow UI. This permits retry without
accepting the deployment as successful. A run with missing or mismatched context
cannot release the target. Repeating a completed request or observing an older
receipt never reacquires the target or releases a later deployment's reservation.
Agents observe the failed attempt before requesting a rerun, so the engine can
confirm the original inputs that GitHub will reuse. Generic dispatch and runtime
refusals return the specific check code to the agent and persist it for the UI.

Bindings without `delivery_contract` preserve their serialization and legacy
verification. Version-2 components cannot satisfy a legacy receipt, or vice versa.
Downstream nodes consume existing release lineage with the contract digest
checked against the accepted predecessor. Changing an in-flight flow requires a
plan amendment; this engine change does not rewrite accepted plans.

## Flow UI

Node cards show failed or waiting checks with the reason and next action.
Successful checks and technical codes are under **Check details**. Timestamps
show when results were recorded. Admission failures, missing required inputs and
execution blockers use the same display. Runtime verification failures persist
in the execution ledger after the agent's tool response ends. A successful
observation clears the current failure, and earlier attempts do not appear as
current results.
