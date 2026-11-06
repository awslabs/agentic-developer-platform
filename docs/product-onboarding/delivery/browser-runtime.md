# Product browser runtime integration

The Product browser suite runs only through the maintained, main-only
`e2e-new-ui-playwright.yml` workflow. Code checks are not live acceptance. The
later verification node owns the matched deployment, browser execution, private
packet publication and cleanup. The human reviews one B02 packet revision; this
integration adds no human approval gate.

For B02, dispatch with `product_source_revision` and the matched frontend release
inputs, leaving `product_v11=false`. The workflow selects only Product create,
conflict/denial and evidence/cleanup from the B02 scenario inventory. It rejects
missing, duplicated or unrelated collected cases before publishing the packet.
This core Profile checkpoint does not require the later onboarding fixtures or
enable onboarding. An unpinned ordinary UI run retains the full suite; the
explicit `product_v11=true` run selects its five integrated onboarding cases.

## Browser evidence producer

`tests/e2e/product_onboarding/product_v02_record.py` collects the actual pytest
setup, call and teardown outcomes for all three Product scenarios. Browser tests
record independent Product API response bodies, viewport artifacts and cleanup
receipts. Session completion writes private per-case files, separate UI/API
reports, a cleanup inventory and `test-results/product-v02-inputs.json`. Missing
cases remain `not_run`, partial cases remain `interrupted`, and failed setup or
cleanup cannot become a pass. Bearer headers are not copied to these files.

The existing packet publisher checks these inputs against the separately produced
JUnit and scenario inventory. It still requires signed deployment identities,
independent runtime observations, both viewport traces, every cleanup receipt and
successful private upload before exposing a packet digest. Missing metadata is
not filled with the checkout revision or a success default.

## Protected fixture lifecycle

`tests/e2e/new_ui/product_fixture_provider.py` connects the three cases to real
Cognito sessions, an encrypted S3 recovery store and existing service APIs. The
owner and independent API observer authenticate separately as the same ordinary
person, because newly created Products are owner-private. A different ordinary
person in another tenant supplies the nonmember session. Administrative identity
readback verifies both canonical user mappings before claiming the fixture.

The owner workspace must be a dedicated `eval-regression-` workspace containing
only the selected ordinary owner. Its membership starts active, with no team
bindings. The provider uses the existing revision-guarded hierarchy removal API
and canonical member-add API; it journals restoration before revocation and
restores the baseline before retirement, including after assertions or lost
responses. It refuses to overwrite an externally changed membership. The shared
test administrator controls setup but is never the person revoked by a case.

The store implements the existing `load(run_id)` / `save(run_id, document,
manifest=...)` contract with one atomic encrypted document per run, separate
case intents, conditional writes and a fixture ownership claim. A fresh holder
invalidates a previous writer during explicit recovery. Original create keys and
receipts survive interrupted runners; no replacement Product intent is invented.
The workflow queues against the maintained environment concurrency group. Do not
run an out-of-band recovery concurrently with the owning workflow.

Retirement signs the retained V01 request contract:
`POST /internal/v1/product-evaluation/retire`, with tenant, actor, run, source,
original idempotency key and the reconciled Product/Project IDs. It uses a
separate, exact-account browser retirement role, never the browser checks role as a
substitute. The owning service must enforce the V01 evaluator capability,
ownership, history-retention and shared-resource guards. The adapter neither
adds a public deletion API nor changes those guards. Ordinary public reads and
bounded discovery then verify owned absence and unchanged other Products.

The gateway registers the owning-service implementation in
`src/products/evaluation_cleanup_routes.py` and `src/products/evaluation_cleanup.py`.
It authenticates the existing API Gateway IAM provenance, the fixed V01 evaluator
or recovery registry identity, or the dedicated browser retirement identity
(`BROWSER_REGISTRY_ID` in the route), and the
`internal:product-fixture:retire` capability.
The evaluator identity must match the complete caller role/account configured in
`ADP_PRODUCT_EVALUATOR_ROLE_ARN`; the separate recovery identity must match
`ADP_PRODUCT_RECOVERY_ROLE_ARN`. Both are backend environment settings maintained
by the deployment owner. Missing configuration grants no authority for that path,
and one identity cannot use the other's role. The browser identity must match
`ADP_PRODUCT_BROWSER_RETIREMENT_ROLE_ARN`,
supplied by the `browser-retirement-role-arn` key in the existing runtime-proof
ConfigMap. That setting must equal the private browser configuration's
`retirement_role`; neither the request body
nor the registry capability alone selects an authorized role. Original-instance-
lost recovery uses the existing V01 recovery runner and its original owned intent,
not an evaluator session or replacement create key.
Retirement closes only the original owned binding, retains Products, Projects and
operation receipts, and appends a cleanup history entry. Repeated original-key
recovery returns the same receipt without adding another history entry.

The conflict scenario treats two denials separately: a nonmember Product read
must return 404, while a revoked tenant session must return the canonical
403 `tenant_membership_revoked` response. Both assertions reject saved-field
disclosure; production authorization is unchanged.

All cases have provider-owned teardown, including conflict denial. The workflow
also runs recovery after pytest and accepts no packet digest unless that cleanup
step succeeds. An interrupted run can be selected by `product_recovery_run_id`;
use its original source, target and protected configuration. This restores and
retires existing intents, not a new browser pass. A subsequent fresh run may
start only after the fixture claim is released.

### Private configuration and deployment prerequisites

The provider reads only the environment's fixed Secrets Manager entry
`adp/<environment>/gateway/product-browser-fixtures`. Maintain it privately with:

- `base_url`, `tenant_id`, `actor_id`, `owner_subject`, `nonmember_id`,
  `nonmember_subject` and `nonmember_tenant_id`, matching actual identity readback.
- `credentials.owner` and `credentials.nonmember`, each containing `username`,
  `password`, `cognito_user_pool_id` and `cognito_client_id`. These are fixture
  credentials, never public examples or evidence fields.
- `state_bucket` and `state_kms_key_id`, permitting encrypted conditional writes
  only under `cli-uplift-eval/state/product-browser/`. Retain run documents after
  cleanup; do not remove a claim to bypass unfinished cleanup.
- `retirement_role` and `internal_invoke_url`, naming the dedicated browser retirement
  role and regional API Gateway stage. Its IAM authentication must map to V01's
  protected retirement capability. No role or policy is provisioned by this suite.
- `plan_revision`, `requirements_revision` and `backend_release` (its `run_id`,
  `attempt` and `workflow_revision`). All revisions are immutable commit IDs.
- For selected B03 scope checks only, `scope_editor` identifies a separate ordinary
  team member (`actor_id`, `subject`, `team_id`) with matching
  `credentials.editor`. `scope_inputs` holds exactly two distinct authorized
  GitHub-installation repository selections (two repositories on the same
  installation are valid), each with `record_revision` and
  `release_target_date`. They are protected configuration, never workflow
  dispatch overrides or public fixture values. The runner creates and retires
  only its own Product and delivery scopes through the existing protected APIs.
  The scope case creates its Product under the plain `eval-product-<run_id>`
  name, because the reviewed V01 create guard accepts a fixed set of case
  suffixes and that guard is not relaxed for B03. The `scope` case key, state
  document and cleanup receipts are unchanged.

`tests/e2e/new_ui/product_fixture_bootstrap.py` is the read-only preflight for
exactly those contents. A fresh B02 run evaluates it before the fixture claim,
before any membership change and before any browser: an unpublished or partial
configuration reports the refused read and the complete list of documented keys
instead of a raw client error or a `KeyError` during fixture construction, and
the protected `backend_release` must equal the pairing selected for the run
(`PRODUCT_BACKEND_RELEASE_RUN_ID`, `_ATTEMPT`, `_WORKFLOW_REVISION`) so a
repaired source is only ever qualified against the API release deployed for it.
No run identity is pinned in portable code. The preflight creates, deletes and
mutates nothing - no user, workspace, membership, password or secret version -
so it needs no authority beyond the browser checks identity's existing
`GetSecretValue`; live identity and login proof, run claims, interrupted-run
recovery and always-run teardown stay with the provider. Fresh B03 runs also
preflight their editor and two scope inputs before claiming state; recovery
deliberately does not require fresh source inputs so it can restore a revoked
owner using the original durable claim. V11 retains its own fixture contract.

### B03 fixture setup and correlated run

B02 keeps its isolated `eval-regression-` workspace and defaults. For B03,
the fixture owner must either use an isolated workspace with a genuinely
authorized GitHub installation, or select `scope_fixture_mode=connected_scope`
in the protected configuration for an existing, connected fixture tenant. The
connected option does not create a GitHub App or transfer a connection from
another tenant. It is limited to dedicated ordinary owner and editor logins
(`eval-regression-` prefix), an independent nonmember in another tenant, and
run-owned Product, Projects, source associations and reversible memberships.
Never use a customer identity or a tenant whose membership cannot be restored.

1. Through the existing administrator identity APIs, read the chosen tenant at
   `/api/api/admin/identity/organizations/{tenant_id}`, its users at
   `/api/api/admin/identity/organizations/{tenant_id}/users`, and the distinct
   nonmember tenant. Confirm the owner and editor's canonical actor IDs, Cognito
   subjects, ordinary `member` roles and dedicated logins; confirm both
   members share the dedicated B03 team and record their original membership
   state. Do not create or modify other tenant users. If a dedicated fixture
   login does not exist, use the existing ordinary identity provisioning
   process; do not repurpose a shared user. The
   supported creation route is `POST /api/api/admin/identity/organizations/{tenant_id}/users`;
   pass `role=member` and the dedicated `team_id` explicitly in
   `UserCreateRequest`, then use the existing Cognito provisioning and repeat
   the readback. In an older connected tenant, omitting `team_id` may select a
   nonexistent synthesized default team and return 404. Discover the actual
   team through the paginated
   `GET /api/admin/organizations/{tenant_id}/departments/{department_id}/teams`
   route; direct `GET /api/admin/organizations/{tenant_id}/teams/{team_id}`
   is not a supported lookup. This is explicit fixture-owner preparation, not
   an automatic browser-run mutation. Once created, do not recreate identities.
2. Authenticate as the ordinary owner. On an owned Product and Project, check
   `GET /api/v1/products/{product_id}/projects/{project_id}/sources` with
   `connection_kind=github_installation`, the selected `connection_id` and
   bounded pagination. Confirm both repositories are actually discoverable on
   that tenant's installation, not merely configured in another tenant. Select
   two different repository `native_id` values and two different ISO calendar
   dates. Do not fabricate a selection, register an App as hidden setup, or
   weaken source authorization. The browser provider repeats this discovery
   before writing source bindings.
3. In the existing private Secrets Manager entry, supply the above actor,
   subject and team bindings, their private `credentials.owner`, `.editor` and
   `.nonmember`, `scope_inputs` (two `selection` objects with `kind=repository`,
   `provider=github`, `connection_ref={kind: github_installation, id: ...}`,
   `namespace`, `native_id`, `record_revision`, `release_target_date`), and
   `scope_fixture_mode=connected_scope` only for the connected option.
   Retain the existing base URL, encryption store, retirement role, plan and
   requirements revisions. Never publish the secret, credentials, tenant IDs,
   repository IDs or actual release receipts in this public document.
4. After the reviewed B03 repair is merged, use the existing
   `gateway-deploy.yml` GitHub Actions workflow to publish that exact main
   ancestor to dev, not an older B02 image: in Actions select Gateway Deploy,
   Run workflow on `main`, `environment=dev`, and
   `manual_source_revision=<reviewed-main-commit>` (leave Product API case
   selection at its default). Verify its signed backend receipt, schema head
   and independently observed API artifact. Update only the
   protected `backend_release` pairing (`run_id`, `attempt`,
   `workflow_revision`) to this verified `gateway-deploy.yml` receipt; do not
   substitute a workflow run number or the checkout SHA for a receipt.
   In Actions select Gateway Frontend Deploy on `main` through the accepted
   correlated dev release: `environment=dev`,
   `adp_source_revision=<same-reviewed-main-commit>`,
   `adp_definition_revision=<reviewed-workflow-commit>`,
   `adp_correlation=<accepted-run-correlation>`, and
   `product_cases=live_scope_isolation,live_scope_denials,live_scope_history_cleanup`.
   Use the already accepted binding, not invented input values. Keep its source,
   definition revision and correlation bound to that release;
   this workflow publishes the frontend and calls the maintained
   `e2e-new-ui-playwright.yml` with its own run, attempt and workflow revision.
   Do not independently dispatch browsers or override the fixed inventory.
5. Inspect the three separate API, installed-CLI and served-browser outcomes,
   the signed backend/frontend and served-byte observations, original create
   and compensation intents, membership restoration and protected retirement
   receipt. A missing, skipped, failed or uncleaned case prevents the private
   B03 packet from completing. On interruption, use the existing
   `product_recovery_run_id` with the original run and protected source and
   target; reconcile before another fresh claim. Retain prior attempts and
   encrypted documents. The later verification owner publishes the private
   packet and the human reviews its exact revision; code-only tests or PR
   publication are not live acceptance.

The fixture owner provisions these dedicated identities and the protected
configuration. The deployment owner supplies a matched backend containing the
V01 retirement route and its exact-role/account authorization, plus the selected
frontend release. These are deployment prerequisites, not claims that V01 is
merged, installed or qualified by a local test. Missing APIs or permissions fail
closed; there is no archive, SQL or pod-execution fallback.

Backend evidence comes from the signed release store and independent runtime
inspection; frontend bytes are checked against their signed inventory before
Product mutation. The collector binds that context to the exact workflow run and
source. Controlled tests exercise provider construction, all three fixture-to-
collector paths, durable recovery and refusal behavior; they do not establish
live acceptance. Durable run intents are separate from the packet's prior-attempt
evidence inventory, whose production history integration remains outstanding.

## Native frontend observation and completion

For native B02 delivery, the accepted frontend release binding selects
`product_cases=live_browser_create,live_browser_conflict_denial,live_browser_evidence`
on `gateway-frontend-deploy.yml`. After publishing the frontend and signed release,
that workflow calls the existing browser workflow with its immutable source,
release run, attempt and workflow revision. The parent deployment succeeds only
after browser checks, cleanup and private packet publication succeed. The engine
then independently verifies the served frontend. No separate dispatch or human
deployment approval is needed.

The native flow path is distinct from the workflow-local asset check:

1. The `adp-dev-gateway-frontend` manifest entry selects `gateway-frontend`, the maintained frontend
   publisher, a verified connection, immutable source/workflow revisions and the
   approved physical target. The existing enabled `adp-dev-gateway` entry selects
   only `gateway-backend`; it does not authorize a frontend dispatch.
2. The frontend publisher accepts the engine's correlation and revision inputs,
   rejects moved or malformed pins before checkout, checks main ancestry, and
   uses evidence scripts from the workflow revision. Engine-correlated runs
   publish a signed workflow-context object and a separate signed frontend
   release object. Uncorrelated manual and push runs preserve their existing
   release-only behavior and do not acquire a new cluster-read prerequisite.
   The optional Product selection defaults to `none`. Selected B02 runs require
   the complete fixed browser inventory, dev and valid engine correlation before
   publication; the signed context records that selection. The API inventory
   remains restricted to the backend workflow. Partial inventories and arbitrary
   workflow inputs are refused.
3. The context publisher verifies account and cluster identity. The evidence
   store and `WorkflowReceipt` accept the frontend context name explicitly, not
   arbitrary workflow names. `deployment_controller.py` and
   `operations_runtime_evidence.py` bind context, source, attempt and release.
4. `PilotRuntimeReader.frontend` reads the distribution through the selected
   connection and hashes the served bytes against the signed inventory. Missing
   permissions, a wrong account, a disabled distribution or wrong bytes fail.
   The controller settles only fresh, matching runtime evidence; browser workflow
   success cannot stand in for this observation.

The frontend entry is enabled in the packaged manifest with an immutable baseline
source/workflow pin, the existing verified connection and the same physical
Gateway namespace target as the backend entry. It permits only the frontend
component and `environment=dev`; it does not broaden the backend entry or grant
rollback. The accepted operations release binding must select this entry and its
reviewed definition predecessor when advancing to a later merged source. Runtime
authority, main ancestry, signed context and served-byte checks still apply.

The existing `eks-namespace` resolver remains the target authority. Distribution
selection comes from the selected environment's protected
`/adp/<environment>/gateway/cloudfront-id` parameter, read through that connection;
the deployment owner must keep it bound to the reviewed distribution. No new
resource kind, connection policy or IAM permission is introduced. The manifest
and local resolver tests establish a reachable code path, not deployed authority
or live acceptance. The deployment/connection owners still qualify the exact
parameter/distribution reads through each selected identity before the live run.

### Minimal maintained permission prerequisites

The browser checks role and native flow connection role are different identities.
Qualify each through its own selected connection; an operator's readback or a
successful browser checks role does not prove the flow role has permission.

Beyond the existing verified-connection, signed-store and cluster-read contract,
the frontend observer requires exactly these additional reads, scoped to the
selected parameter and distribution. These are fictional examples, not targets:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-east-1:123456789012:parameter/adp/example/gateway/cloudfront-id"
    },
    {
      "Effect": "Allow",
      "Action": "cloudfront:GetDistribution",
      "Resource": "arn:aws:cloudfront::123456789012:distribution/E0000000000000"
    }
  ]
}
```

For engine-correlated runs, the frontend publisher's new context step also needs `eks:DescribeCluster` on
the exact cluster ARN already named by its protected configuration, and its
existing encrypted evidence-store write permission must cover the
`context/gateway-frontend-deploy.yml.json` key for its run/attempt. The native
reader needs the corresponding signed-object/version reads. These prerequisites
do not require CloudFront updates, wildcard resources, IAM administration or
pod execution. Deployment/connection owners qualify the permissions later;
code review must not claim those live checks already passed.

### Browser retirement authority

`platform/evaluator-infra/product-browser` provisions a dedicated role whose only
service operation is the protected retirement route. Its trust names the existing
browser checks role, and that role receives only the corresponding `AssumeRole`
grant. The EC2 evaluator and its recovery orchestrator keep their existing trust.
The module exports a registry item binding the new role's immutable IAM ID to
the browser retirement identity and capability, plus the ConfigMap key/value.
Registration must use a conditional create after checking the identity is absent;
never overwrite a different registration. The Gateway receives only `GetRole` on
this new role so it can reject deletion and recreation under the same role name.

Apply the reviewed Terraform plan in its own state, install the registry item and
ConfigMap setting, and roll out the matching Gateway source before qualifying the
fixture. The browser checks role's signed-release, runtime-observation and
encrypted-state permissions, the runtime-proof allowlist, and a matched frontend
and backend release remain separate prerequisites. This module does not grant
those permissions or establish browser acceptance.
