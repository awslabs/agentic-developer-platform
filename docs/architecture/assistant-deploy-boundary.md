# Assistant rollout boundary: push-workflow inventory

Owner story: #6932 (gateway/data-path and worker-image boundary). #6931 extends
this inventory to IAM and runtime changes; #6936 extends it to the frontend.
Design requirement: `docs/architecture/adp-assistant-6929.md`, section
"Delivery dependencies and merge boundary".

Components audited and their source roots:

| Component | Source roots |
|---|---|
| gateway | `modules/gateway/src/**` |
| chat worker | `modules/agent-factory/agent/**` |
| agent-worker image | `modules/agent-factory/agent/**`, `modules/agent-factory/agent-worker-image/**`, `modules/agent-factory/codex-reviewer/**`, `modules/agent-factory/codex-harness/**` |
| frontend | `modules/gateway/frontend/**` |

## Inventory

Every workflow under `.github/workflows/` whose `on.push.paths` cover any of the
roots above (all trigger on `main`). "Trigger paths" lists only the
component-relevant patterns; see each file for the full list.

| Workflow | Trigger paths (component-relevant) | Mutates | Guard decision |
|---|---|---|---|
| `gateway-deploy.yml` | `modules/gateway/src/**` | `deploy-backend`: builds `adp-gateway` via CodeBuild, runs Alembic, rolls the image onto EKS namespace `adp-gateway`; `deploy-frontend` runs on `workflow_dispatch` only | Guarded, component `gateway`, target `adp-gateway-deploy-<env>` |
| `chat-agent-deploy.yml` | `modules/agent-factory/agent/**` | `build-and-deploy`: builds `adp-chat-agent` via CodeBuild and updates the `chat-agent-worker` ScaledJob in `adp-gateway-agents` | Guarded, component `chat-worker`, target `adp-chat-deploy-dev` |
| `agent-worker-image.yml` | `modules/agent-factory/agent/**`, `modules/agent-factory/agent-worker-image/**`, `modules/agent-factory/codex-reviewer/**`, `modules/agent-factory/codex-harness/**` | `build`: builds `adp-agent-runtime` via CodeBuild (build only); `deploy`: `rollout-worker-image.py` pins the digest in SSM and patches `agent-scaledjob` in `adp-agents` | Guarded on the `deploy` job only, component `agent-worker`, target `adp-worker-deploy-<env>`; build and test jobs stay unguarded |
| `gateway-frontend-deploy.yml` | `modules/gateway/frontend/**` | `publish`: builds the SPA, syncs S3, invalidates CloudFront | Out of scope for #6932: no protected assistant paths exist under `modules/gateway/frontend` yet; #6936 must add a `frontend` component and guard this job before assistant UI merges |
| `gateway-ci.yml` | `modules/gateway/src/**`, `modules/agent-factory/agent/src/complex-task-chat/**`, `modules/agent-factory/agent/package*.json`, `modules/agent-factory/codex-reviewer/package.json` | Lint and tests; `build` runs the smoke buildspec under the non-publishing PR CodeBuild role (no image is published, nothing deployed) | Build-only / test-only, no guard |
| `e2e-chat-playwright.yml` | `modules/gateway/frontend/**`, `modules/agent-factory/agent/**` | Runs Playwright against the already-deployed environment with read-only `trusted-checks` identity | No mutation, no guard |
| `gitlab-integration-tests.yml` | `modules/agent-factory/**` | Contract and live-fleet tests with read-only `trusted-checks` identity | No mutation, no guard |
| `platform-upgrade-tests.yml` | `modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh`, `modules/agent-factory/agent/k8s/chat-scaledjob.yaml`, `modules/agent-factory/agent/package*.json`, `modules/agent-factory/agent/src/complex-task-chat/sweepers/**`, `modules/agent-factory/agent-worker-image/entrypoint.py` | Unit and Terraform contract tests with a mocked AWS provider | Test-only, no guard |
| `codex-harness-tests.yml` | `modules/agent-factory/codex-harness/**`, `modules/agent-factory/agent-worker-image/lib/task*.py`, `modules/agent-factory/agent-worker-image/lib/codex*.py`, `modules/agent-factory/agent-worker-image/Dockerfile`, `modules/gateway/src/agentauth/task*.py`, `modules/gateway/src/tasks/**` | Unit tests | Test-only, no guard |
| `orchestration-review-contract-tests.yml` | `modules/agent-factory/agent-worker-image/lib/review*`, `modules/agent-factory/agent-worker-image/entrypoint.py`, `modules/gateway/src/orchestration/review*`, `modules/gateway/src/agentauth/artifact_service.py`, `modules/gateway/src/agentauth/review_upload.py` | Unit tests | Test-only, no guard |

`modules/gateway/tests/orchestration/test_assistant_deploy_boundary.py` derives
this set from the workflow files at test time and fails when a workflow covering
one of the roots is missing from this table.

## How the guard works

`scripts/check-assistant-deploy-boundary.sh` runs in each guarded job after
checkout (`fetch-depth: 0`) and before any AWS credential or kubeconfig step. It
receives the component (`gateway`, `chat-worker`, `agent-worker`), the exact
deployment environment name as `ADP_ASSISTANT_DEPLOY_TARGET`, the candidate
commit, and the approval inputs below. Each component has its own protected path
list in the script; `agent-worker` protects
`modules/agent-factory/agent/src/complex-task-chat` and
`modules/agent-factory/agent/k8s/chat-*`, the assistant surfaces that ship inside
the shared worker image.

Approval is granted per target by setting two variables on the protected GitHub
deployment environment after the source has been approved for that target:
`ADP_ASSISTANT_APPROVED_TARGET` (the environment name) and
`ADP_ASSISTANT_APPROVED_REVISION` (a full 40-hex commit). The approval must be
an available ancestor of the candidate, and the candidate's protected files must
be identical to the approved revision's; otherwise the job fails with
"Candidate contains held assistant changes". A later unrelated push therefore
cannot carry an earlier held assistant change.

A `workflow_dispatch` has no bypass. It passes either on the persistent approval
above or on the `adp_approved_revision` input (full 40-hex commit), which the
workflows forward as `ADP_ASSISTANT_DISPATCH_APPROVED_REVISION`; the input
authorizes that run only and does not update the persistent approval.

When no approval applies to the target, the guard does not disable the workflow
globally. A step before the guard resolves `ADP_DEPLOYED_BASELINE` with the job's
`GITHUB_TOKEN`. For the worker workflows (`actions: read`), it lists the last ten successful runs of the
same workflow file on the target branch and, querying each run's jobs in order,
takes the `head_sha` of the first run whose guarded deploy job (`Build TS Image and Update Chat ScaledJob` with its `Update chat
ScaledJob manifest` step, or `Roll out Agent Runtime Image`) concluded `success`.
A successful run whose deploy job or rollout step was skipped is not a deployment
and is passed over. If no protected file changed between that baseline and
the candidate, the guard prints "No assistant changes in this candidate ...
unrelated deployment continues" and exits 0; otherwise it refuses. Because the
baseline is what was deployed rather than the previous push, a refused push
cannot be carried live by a later unrelated push. An empty, malformed or
non-ancestor baseline refuses with a message that the deployed baseline is
unknown and that recording `ADP_ASSISTANT_APPROVED_*` (or dispatching with
`adp_approved_revision`) is the way forward; there is never a fallback to
`github.event.before`.

Bootstrap: the first run of a brand-new workflow has no successful deploy job and
therefore no baseline. The operator records `ADP_ASSISTANT_APPROVED_*` on the
target environment or dispatches once with `adp_approved_revision`; the code
does not special-case this.


### Gateway source selection and deployment evidence

`gateway-deploy.yml` accepts a separate `manual_source_revision` dispatch input:
an exact lowercase 40-character commit, already an ancestor of the dispatched
`main` revision. It cannot accompany any engine correlation/source/definition
input. Engine delivery retains its complete validated tuple. Every checkout,
CodeBuild source, image tag, reusable migration input and release record uses the
selected revision. Jobs retain their main-only protected environments and OIDC
roles. Selecting an ancestor does not approve held assistant changes: the
unchanged assistant guard compares that candidate against the verified deployed
source, or against a normal explicit target-specific approval.

The Gateway loads its guard, receipt resolver and backend evidence writer from
`github.workflow_sha` into runner temporary storage. A historical source cannot
supply an obsolete or missing guard. The source checkout remains the selected
commit, with full history for ancestry checks.

The Gateway baseline resolver examines actual backend jobs, including successful
backend jobs in runs whose later frontend/migration jobs failed. It uses the
exact run attempt, requires a successful rollout and completed backend job, and
refuses a later incomplete rollout instead of using an older baseline. Lookup is
bounded to 1,000 records per collection; exhaustion refuses. A different target
in that history also refuses rather than silently guessing a baseline.

New backend releases finish by writing a GitHub Deployment with task
`adp-gateway-source-v1`, the protected environment, exact source SHA, and the
release evidence payload. Its successful status links the exact run attempt.
The durable payload binds repository, run, attempt, workflow path and definition,
account, region, cluster/namespace, source, and image digest. Publication compares
the digest with the deployed release output. Baseline resolution checks those
bindings and the completed backend job, independently of Actions artifact expiry.
`deployments: write` is confined to that protected backend job.

A legacy workflow without the receipt publication step can resolve its existing
`adp-release-gateway-backend-<attempt>` Actions artifact. Exactly one nonexpired,
bounded artifact must have the authenticated run/repository/head metadata, its
GitHub SHA-256 digest must match the downloaded archive, and its sole
`release.json` must satisfy the same release bindings. A maintained workflow
missing its durable receipt cannot fall back to this legacy path. Missing,
ambiguous, expired, or mismatched evidence refuses; the workflow's `head_sha`
is never substituted for deployed source. Neither a receipt nor an artifact
constitutes assistant rollout approval.

A normal explicit assistant approval skips baseline lookup and is independently
validated by the guard, preserving the documented first-deployment bootstrap.
Do not provide that approval merely to recover missing evidence. Inspect the
failed rollout or recover its authenticated deployment evidence first.
