# Deployed API/client integration qualification handoff

This is an operator recipe, **not a live qualification result**. The gateway
PostgreSQL fixture checks the same controller/runner/acceptance path with mocked
GitHub, Kubernetes and AWS transports. Run the local check from the repository
root (with the gateway test dependencies and PostgreSQL fixture prerequisites):

```bash
AWS_CONFIG_FILE=/dev/null AWS_ACCESS_KEY_ID=fixture AWS_SECRET_ACCESS_KEY=fixture \
  modules/gateway/.venv/bin/pytest -q --disable-warnings \
  modules/gateway/tests/orchestration/test_repository_deployed_source.py \
  -k 'product_bound_api_and_client_deployed_probe_settles_exact_sources or product_bound_deployed_human_gate_requires_fresh_evidence_and_approval'
```

The fixture credentials above are local placeholders; never use them for a
target operation. Before live qualification, the root operator must verify the
selected connection and exact account/role, confirm the Product and epic scope,
authorize the target and provision a supported runner with network access and
scoped credentials. Qualification repositories currently have only baseline
`ubuntu-latest` CI, no secrets/variables/runners; `arc-runner-deploy` belongs to
the platform repository only. Restricted EKS access cannot be assumed from a
GitHub-hosted runner. Keep real target identities and proof in protected
configuration; follow the canonical deployment guide for approval and access.

## Reviewed release inputs

Prepare **two** enabled `DeploymentManifest` entries, one per story repository:

| Field | API entry | Client entry |
| --- | --- | --- |
| `entry_id` | `qualification-api` | `qualification-client` |
| `component_selectors` | `qualification-api` | `qualification-client` |
| `artifact_revision` | Exact reviewed API merge SHA (40 hex characters) | Exact reviewed client merge SHA (40 hex characters) |
| `workflow.path` | Reviewed API deployment workflow | Reviewed client deployment workflow |
| `workflow.definition_revision` | Pinned workflow SHA | Pinned workflow SHA |
| `connection_id`, `resource_kind`, `resource_id` | Registered, authorized target | Compatible registered target |
| `verification_adapter` | `qualification-api-runtime-verification` | `qualification-client-job-verification` |

Set `schema_version` to `1`, use the reviewed `workflow.allowed_inputs` and
`correlation_input`, and keep each entry's `status` enabled only after the
workflow, revision, verification adapter and target are available. The API
release artifact must identify its actual API merge and image digest; the client
release artifact must identify its actual client merge, command/CLI digest and
qualification job. Each carries its own repository ID, workflow revision,
run/attempt, target and producer evidence reference. A merged PR or a single
shared build is not a deployment receipt. If the repaired client changes its
`artifact_revision`, review and release an updated client manifest entry before
deploying it; do not edit historical receipts to point at the new build.

## Accept and run

1. In the Product-bound flow, obtain separate successful, server-owned deployment
   actions for API and client. Record the two **operation keys** from the
   authenticated `deployment_receipt` records in private operator storage,
   along with each actual revision, target, observation and expiry. Reject an
   incomplete receipt, an expired one or a target mismatch. Do not substitute a
   GitHub merge event, workflow URL or caller-authored receipt for these keys.
2. In the accepted evaluation specification, list both predecessor graph
   addresses, repository IDs and `evidence_kind: deployment`; set
   `environment.deployment_receipt_keys` to the two distinct keys, and pin the
   registered `connection_id`, `target_contract_hash`, runtime contract and
   compatible physical targets. Pin the harness repository, definition and
   source revisions, workflow path and digest. Select `acceptance_mode: machine`
   or `human` explicitly; the plan policy must agree. The `source_manifest`
   sent to the probe must name **both** exact deployed revisions, components,
   repository IDs, receipt keys, target contract hash and runtime artifacts.
3. As an authenticated human plan approver, submit the complete
   `EvaluationAcceptanceRequest` to
   `POST /orchestration/flows/{flow_id}/evaluation/preview`, with `node_id`,
   `expected_plan_version`, `expected_plan_hash`, `specification`,
   `authorize_evaluate: true` and a reason. Check the returned snapshot and
   unchanged budget/plan. Send the same request with `expected_snapshot` set to
   that preview's `snapshot` to
   `POST /orchestration/flows/{flow_id}/evaluation/accept`. A changed snapshot
   requires a new preview; no worker-provided input can replace this decision.
4. Run the scheduled dispatcher and execution runner. The pinned probe workflow
   must independently read the actual API service and completed client job on
   the authorized target, execute the real integration criteria and emit their
   results and both runtime readbacks. The example adapter
   `modules/gateway/tests/orchestration/fixtures/deployed_probe_emitter.py`
   illustrates the JSON envelope; it does **not** perform the live test or
   authorize the result. The producer verifies the workflow run, attempt,
   correlation, exact source manifest digest, target and evidence artifact.
   Machine mode settles a valid pass; human mode stays `awaiting_gate` until
   the current evidence is ready and a permitted human approves it. Expired
   evidence must be re-proven before approval.
5. After a B correction, deploy the reviewed new B build. The old combined
   verdict cannot settle the new A/B set. If unchanged A's original receipt
   expires during repair, the scheduled evaluation may issue a bounded,
   evaluation-owned A refresh only after current authorized runtime re-observation
   proves the same revision, artifact and target. This creates a new proof key
   tied to the original key and correction, without rewriting original evidence;
   changed A runtime or withdrawn authority blocks the probe. The next probe
   must use the fresh exact set (renewed A and newly deployed B). Preserve the
   original failure and operation/claim across retries; never manually mint a
   receipt or repeat a dispatch solely because its response was lost. Automatic
   correction is **machine-mode only**; human-mode evidence requires a fresh
   attributed human approval and does not authorize automatic correction.

## Artifact and workflow contract for the live owner

- The API deployment entry targets `eks-deployment`, with a resource ID shaped
  like `cluster-example/qualification/api`; the client targets `eks-namespace`,
  with `cluster-example/qualification`. These are fictional shapes, not approved
  target names. The registered connection and policy must allow the actual
  physical target. Both workflow definitions must accept the approved
  `customer_account_id`, `customer_user_id` and `customer_aws_label` inputs;
  the engine resolves their values from the registered connection and human
  principal and checks the manifest's permitted values before dispatch.
- Each deployment workflow publishes a GitHub artifact named
  `adp-deployment-context-<workflow_filename>-<attempt>` containing **only**
  `deployment-context.json` (the authenticated run context), and one named
  `adp-release-<component>-<attempt>` containing **only** `release.json`. The
  release JSON must bind repository ID, run/attempt, workflow path/revision,
  source revision, account/region/resource and component to that same run.
  Pin the real merge SHA in both the release and manifest. See the
  [context provider](../../../modules/gateway/src/orchestration/deployment_workflow_provider.py),
  [release provider](../../../modules/gateway/src/orchestration/deployment_release_provider.py)
  and [release/receipt models](../../../modules/gateway/src/orchestration/deployment_runtime_contract.py).
- Before accepting the evaluation, create and verify a **real evaluation issue
  in the harness repository**, bound to the Product flow. It is distinct from
  the A and B story issues. The harness workflow must be dispatchable at its
  pinned definition/source revisions, declare **only** `workflow_dispatch`,
  use a non-cancelling concurrency group and the exact run-name
  `${{ format('ADP deployment {0}', inputs.adp_correlation) }}`.
  Probe transport inputs are `adp_correlation`, `adp_source_revision` and
  `adp_definition_revision`; engine inputs are `source_snapshot_hash`,
  `source_manifest`, `specification_digest`, `probe_criteria`, plus the exact
  accepted `probe.inputs` (including `target_contract_hash` and
  `runtime_contract`). The engine enforces the complete input-name set; do not
  add workflow inputs or rely on a mutable default. The probe publishes one
  GitHub artifact named `<probe.artifact_name>-<run_id>-<run_attempt>` (for
  `evaluation-result`, `evaluation-result-<run_id>-<run_attempt>`) containing
  **only** `<probe.artifact_path>` (`result.json` in this example). The JSON
  envelope includes `evidence_schema: deployed-probe-result/v1`, correlation,
  source snapshot hash, source manifest digest, specification digest, target
  contract hash, run ID/attempt, observation time, criteria and both runtime
  readbacks. See the [producer preflight](../../../modules/gateway/src/orchestration/repository_producer.py),
  [probe artifact verifier](../../../modules/gateway/src/orchestration/repository_evaluation_provider.py)
  and [example emitter](../../../modules/gateway/tests/orchestration/fixtures/deployed_probe_emitter.py).

Root owns implementing and reviewing the live sample workflows, private
configuration, runner and target access, and recording live Stage 3 evidence
**after** the gate. No infrastructure or credential change is part of this
handoff; do not infer live acceptance from the local PostgreSQL fixture.

## Local recovery check

From the repository root, with the gateway test dependencies and PostgreSQL
fixture available, run the maintained scheduled-controller cases:

```bash
AWS_CONFIG_FILE=/dev/null AWS_ACCESS_KEY_ID=fixture AWS_SECRET_ACCESS_KEY=fixture \
  modules/gateway/.venv/bin/pytest -q --disable-warnings \
  modules/gateway/tests/orchestration/test_repository_deployed_source.py \
  -k 'test_product_bound_b_correction_restart_reconciles_issue_and_parent or test_repaired_b_requires_a_new_exact_set_probe or test_failed_b_probe_cannot_start_a_correction_after_cycle_budget_exhausts'
```

Expect three restart-link variants, five corrected-B probe variants (including
renewed A after actual expiry and both pass and fail results), and one exhausted
budget refusal. They exercise the scheduled runner with PostgreSQL persistence:
one B correction issue and native link survive lost responses, the second probe
keeps its operation and evaluation claim across restart, and only the current
A/B receipt set can settle. An old failure remains historical; the renewed A
proof retains a verifiable original key, and a failing retest stays attributable
to B. These tests mock GitHub, Kubernetes and AWS; a passing local selector is
not a receipt from the target deployment or a live Stage 3 decision.
