# Platform regression

ADP uses three primary entry points. Existing component PR workflows keep their
required check names; their jobs are also reused by the full scheduled sweep.

| Entry point | Trigger | Purpose |
| --- | --- | --- |
| ADP CI | Regression-infrastructure PRs, reusable calls, manual | Full component test sweep, orchestration contracts and source security scanners. Ordinary component PR checks retain their existing selection and names. |
| ADP Deploy Check | Successful automated main deployments; manual dev diagnostics | Real-token gateway smoke, browser OAuth/session checks and New UI navigation. |
| ADP Regression | 02:17 UTC Monday–Saturday; 02:17 UTC Sunday; manual | Daily coverage or the weekly superset, one coordinated run and combined verdict. |

The two scheduled expressions belong only to `adp-regression.yml`. Sunday runs
the weekly profile instead of a second overlapping daily run. The event's cron
expression selects the profile even when GitHub starts it late. A cron is not a
completion SLA; monitor run freshness separately from runtime.

## Daily and weekly profiles

The daily profile runs:

- The existing offline component workflows: gateway/frontend, agent/context,
  credentials, controls, provenance, orchestration/harness, Lambda, domain/tool,
  release/upgrade, platform and automation contracts. Four chains bound workflow
  fanout; existing gateway test sharding is retained. Its cloud image build is
  excluded from this offline invocation; native component CI still builds it.
- Source Checkov, Semgrep, Bandit, detect-secrets and both maintained npm package
  audits. The scanner commands, versions and baselines come from the existing
  Security Scan definition. Scanner errors or missing output fail; new rated
  high/critical findings fail. Baselines are never automatically updated.
- Gateway smoke, enabled Chat browser journeys, New UI acceptance, real gateway
  API tests, CLI onboarding/budget enforcement/EC2, GitHub/GitLab integration and
  credential adversarial checks.

The daily EC2 scope remains `nightly`, currently E01, C01 and E20–E42: 25 cases,
including hosted coding and tenant isolation. The weekly profile includes that
scope plus capability contrast and the implemented hosted chat, vault, knowledge,
machine, budget and hierarchy lifecycle diagnostics. The versioned selection is
in [regression-profiles.json](../../.github/regression-profiles.json). Additional
fixtures must be supplied before those cases can pass; absent fixtures remain
blocked, and weekly does not claim full CLI acceptance.

Cloud image scanning/SBOM, authenticated engine evaluation receipts, live routing,
autonomous delivery and model-assisted security review retain their separate
qualification workflows. This coordinator does not remove their existing
authorization, implementation or recovery prerequisites, and source scanning is
not evidence of deployed-image vulnerability coverage.

For acceptance after provisioning a new environment, use the separate [new environment E2E smoke workflow](new-environment-smoke.md). Its explicit target contract and curated tests are independent of the nightly coordinator described below.

## Platform targeting and supported environments

The coordinated live pack currently targets ADP deployed on AWS with EKS gateway
observation, using the `arc-runner-org` Actions runner pool. CLI evaluations launch
disposable EC2 instances; browser lanes run Playwright Chromium from Actions.
Runner location, CLI execution location and the deployment under test are separate
choices. The pack does not provide general Azure, GCP, local or non-EKS targeting,
or establish coverage across all browsers and operating systems. Offline component
tests do not establish acceptance of a live deployment.

| Target setting | Meaning and current behavior |
| --- | --- |
| `target_environment` | Selects protected GitHub configuration. Manual runs can select a configured target; schedules always select `dev`. It does not rename AWS resources or provision a deployment. |
| Resource `environment` | Controls resource names and SSM paths. The coordinator supplies `dev` to onboarding, budget and CLI lanes; other live lanes also default to or hardcode `dev`. A target label such as `staging` can therefore refer to resources named `adp-dev-*`. |
| Platform account | Account hosting the tested ADP deployment. Resolved through lane-specific OIDC roles, private bindings and deployment configuration; there is no coordinator-wide account input. |
| Destination account | Separate account used by CLI destination scenarios. Evaluator setup currently requires a distinct destination account even for login; destination roles are optional for login. It does not select the platform deployment. |
| AWS region | Must agree across roles, bindings, endpoints and resources. Several lanes hardcode `us-east-1`; `ADP_DEPLOY_REGION` is used by some identity steps, not as a global region override. |
| Endpoints | CLI bindings use a gateway URL including `/api`; live API bindings use a CloudFront origin without `/api` plus a separate API Gateway URL. Browser lanes need the CloudFront SPA origin. |
| `gateway_deployment` | Key in the private CLI gateway catalog. It selects the gateway's account, region, URL, EKS cluster, namespace, deployment, service, container, selector and ECR repository. It need not equal the GitHub target label. |
| `expected_revision` | Optional full lowercase 40-character deployed gateway commit SHA. Pins the initial snapshot; the final snapshot must match. It does not select or deploy a release. |

These constraints are implemented in the
[coordinator](../../.github/workflows/adp-regression.yml) and its reusable lanes.
For example, [live API tests](../../.github/workflows/gateway-live-tests.yml) set
`ENVIRONMENT: dev` and `AWS_REGION: us-east-1`, while
[credential adversarial tests](../../.github/workflows/credential-binding-adversarial-e2e.yml)
also use `/adp/dev/...` paths. The nightly snapshot and browser credential steps use `us-east-1`; CLI
credential steps default to it unless a caller supplies `region`. Do not treat a different target label or one region variable as
support for an arbitrary resource environment or region: review every selected
lane against the intended deployment first.

## Configuration before activation

For a new target, use the [evaluator credential setup script](evaluator-setup.md)
to provision its base OIDC identity, EC2 profile and gateway-observer access.

Configure the existing protected environments and main-branch restrictions. Do
not put real target identifiers or credentials in public examples.

Manual runs select `target_environment` from the repository's configured GitHub
environments; `dev` remains the scheduled default. Target names must be lowercase
slugs of up to 63 characters, starting with a letter and containing only letters,
digits and hyphens. This selects the protected GitHub environments for live
lanes; their configuration must independently resolve to the intended deployment.
For a target named `staging`, configure `staging`, `adp-checks-staging`,
`adp-browser-checks-staging`, and `adp-deploy-staging` with the corresponding
scoped roles and private configuration in the table below.
Each OIDC role must trust its exact protected environment. Selecting a target
does not create roles, copy credentials, or provision fixtures.

Set `expected_revision` to a full deployed release SHA to pin the initial
snapshot. A failed initial snapshot blocks every live lane; the final snapshot
must match the initial revision.

| Protected GitHub environment | Required configuration and storage type |
| --- | --- |
| `<target>` | **Secrets:** `AWS_CLI_UPLIFT_EVAL_ROLE_ARN` (legacy fallback `AWS_E2E_ROLE_ARN`), `CLI_UPLIFT_EVAL_BINDINGS_JSON`, `CLI_UPLIFT_EVAL_GATEWAY_CATALOG`. Configure fixture references and CLI variable overrides for the selected scope per [CLI setup](cli-uplift-evaluation.md#configure-the-first-run). Snapshots use the same private bindings. |
| `adp-checks-<target>` | **Variables:** `ADP_CHECKS_ROLE_ARN`, `ADP_DEPLOY_REGION` (default `us-east-1`), `ADP_SMOKE_REFRESH_TOKEN_ARN`. **Secret:** `GATEWAY_LIVE_TEST_BINDINGS_JSON`. Supply the provider-specific integration fixtures. The checks role must be authorized for the exact credential references. |
| `adp-browser-checks-<target>` | **Variable:** scoped `ADP_CHECKS_ROLE_ARN`. Supply browser fixture credentials, target SSM URL parameters and network access. The identity step uses `us-east-1`. Daily Chat requires Chat enabled on the deployment; an expected flag does not enable it. |
| `adp-deploy-<target>` | **Variables:** reviewed `ADP_DEPLOY_ROLE_ARN`, `ADP_DEPLOY_REGION` (default `us-east-1`), and `ADP_DB_USER` for onboarding/budget. Supply the database access and fixtures required by the selected workflows. |

GitHub variables and secrets are different namespaces: storing a checks or deploy
role only as a secret will not satisfy a `vars.*` lookup. Credential-reference
variables hold names or ARNs, never token/password contents. The trusted identity
actions use GitHub OIDC with exact environment trust and main-branch restrictions;
provision missing roles through reviewed automation infrastructure, without
substituting ambient or administrator credentials.

### How each live lane resolves its target

| Lane | Resolution and readiness checks |
| --- | --- |
| [Snapshots](../../.github/workflows/_regression-snapshot.yml) and [CLI EC2](../../.github/workflows/eval-cli-uplift.yml) | Selected target's private bindings and gateway catalog, plus CLI variable overrides. Check platform/destination accounts, region, gateway URL, Cognito pool, VPC/subnet/security group, instance profile and EKS observer selectors. Catalog key must match `gateway_deployment`; network access, fixture identity and recovery storage must be ready. |
| [Gateway smoke](../../.github/workflows/gateway-smoke.yml) | Checks role plus [deployment config resolution](../../.github/actions/load-deploy-config/action.yml): explicit action inputs, then `config/deployment.yml` on the checked-out ref, then runtime fallbacks such as STS account. The coordinator supplies no account/resource-environment override. Refresh-token ARN must match the resolved account, region and `adp/<environment>/gateway/smoke-user-refresh-token` name. |
| [Live gateway API](../../.github/workflows/gateway-live-tests.yml) | `GATEWAY_LIVE_TEST_BINDINGS_JSON` explicitly identifies account, both endpoints, machine and native-administrator secrets, and owned organization. Account must match STS; organization ID and exact `eval-regression-*` name are verified before mutation. |
| [Chat](../../.github/workflows/e2e-chat-playwright.yml) and [New UI](../../.github/workflows/e2e-new-ui-playwright.yml) | [URL resolver](../../.github/scripts/resolve-dashboard-url.sh) reads `/adp/${ENVIRONMENT}/gateway/frontend-url`, falling back to `/adp/${ENVIRONMENT}/gateway/cloudfront-domain`; standalone callers may supply `cloudfront_url`. Check the resulting CloudFront SPA origin, authentication fixtures and feature flags. An internal ALB URL is not the browser target. |
| [Onboarding](../../.github/workflows/eval-cli-onboarding.yml), [budget](../../.github/workflows/eval-budget-ratelimit.yml) and [credential adversarial](../../.github/workflows/credential-binding-adversarial-e2e.yml) | Deploy role, resource-environment SSM paths, EKS/database access and owned identities. Coordinated onboarding/budget use `dev`; adversarial hardcodes `dev` paths. Budget derives cluster name `adp-${ENVIRONMENT}-eks-cluster`. Verify mutation and restoration scope before running. |
| GitHub/GitLab integrations | Selected CLI/checks configuration and provider-specific repository, installation, identity and token fixtures. [GitLab workflow](../../.github/workflows/gitlab-integration-tests.yml) uses the checks environment. Base evaluator setup does not create these fixtures. |

Legacy `customer_account_id`, `customer_aws_label` and `customer_user_id` inputs
are rejected by deployment config resolution. Linked inspection/routing roles
are not deployment target selectors. A passing snapshot alone does not prove
that smoke, API, browser and mutating lanes resolve to that same deployment.

`GATEWAY_LIVE_TEST_BINDINGS_JSON` is a protected checks-environment secret holding
identifiers, not credential values. Its shape is:

```json
{
  "account_id": "000000000101",
  "gateway_url": "https://gateway.example.com",
  "api_gateway_url": "https://api.example.com/stage",
  "m2m_secret_name": "adp/dev/gateway/regression-m2m",
  "admin_secret_name": "adp/dev/gateway/test-admin-credentials",
  "organization_id": "owned-regression-organization-id",
  "organization_name": "eval-regression-owned-fixture"
}
```

The account must match STS. `gateway_url` is the CloudFront origin without `/api`;
the API Gateway target is resolved separately. The live API fixture verifies the
configured organization ID and its exact `eval-regression-*` name before mutation.
It no longer selects the first available organization. Use an organization owned
exclusively by this harness. Configure the remaining Cognito/IAM fixtures required
by the gateway E2E package through its existing SSM/Secrets Manager contract.
The native-administrator secret uses `username`, `password`,
`cognito_user_pool_id` and `cognito_client_id`; its password challenge must already
be complete. The checks role needs access to that exact secret and
`cognito-idp:AdminInitiateAuth` on its pool. The harness checks the pool against
gateway discovery before login. Administrator reads, owned fixture mutations and
cleanup use this native identity. Model requests keep the registered machine
identity, whose administrator access must remain denied.

The resolver exports `TEST_BEDROCK_MODEL` from the installer's current runtime
defaults. An optional `test_bedrock_model` binding can select a reviewed Anthropic
foundation or inference-profile ID. The live API module requires this resolved
model instead of retaining a model version after its retirement. Budget and
rate-limit CRUD use a temporary department/team, verify the persisted values and
remove their configurations before removing the hierarchy.

IAM E2E cases additionally require `iam_role_arn`: an account-local
`eval-regression-*` role registered to the owned tenant. Its trust must allow the
checks identity to assume it, and its policy should permit only the target API's
`/agent/*` requests. The harness assumes this role explicitly, sends IAM traffic
through `/agent`, and signs the exact encoded URL, query and body transmitted by
HTTPX. Do not register a platform administration role as a tenant fixture.

The coordinator does not provision a regression deployment, enable Chat, create
the required persistent fixture tenants, or grant missing IAM permissions. Existing
live harnesses do create temporary identities, data and compute. In particular,
onboarding temporarily changes the shared organization-assignment enforcement
flag and must restore it before budget tests proceed. Preflight failures
are actionable configuration failures, not passing coverage. A dedicated enabled
regression target and a separate approved disabled-feature target are still
needed to establish both feature modes. Choosing a disabled-chat target does not
relax the enabled-conversation gate.

## Target record and operator preflight

Keep one reviewed target record in protected configuration or an access-controlled
operator store. This is a **manual record and checklist**, not a supported unified
manifest input or an automated preflight across all lanes. Keep identifiers and
credential references private; do not commit real target records to this guide.

Before dispatch, verify the following for every selected live lane:

| Record and check | Required evidence |
| --- | --- |
| Deployment identity | GitHub target and its four protected environments, intended platform account, region and AWS resource environment. Check STS identity under each lane's scoped role; reconcile deployment config and private binding overrides. Record a distinct destination account where required. |
| Endpoints and workloads | Resolve API, CLI and browser URLs to the same intended deployment. Match catalog EKS cluster/namespace/deployment/service/container/selector and ECR repository to that deployment; record Cognito pool and required SSM paths. |
| Release | Record expected gateway SHA and component deployment receipts for frontend, workers and domains as applicable. Gateway snapshots alone cannot attest those other components. |
| Execution and connectivity | Confirm `arc-runner-org` availability and runner access to required endpoints, AWS APIs and cluster. Confirm the disposable EC2 subnet/security group reaches SSM, S3, Secrets Manager and gateway, with evaluator-required no-ingress rules. |
| Identities and fixtures | Confirm exact OIDC environment trust, scoped IAM/RBAC, native login readiness, smoke token, owned tenant/organization and provider/model/destination fixtures needed by selected scenarios. Check enabled/disabled feature expectations against actual deployment flags. |
| Evidence | Record repository, workflow/run identity, selected modules/profile and target/revision, plus S3 artifact destination, access, encryption and retention per [CI artifact storage](../engineering-insights/ci-artifact-storage.md). The artifact bucket is a separate configuration; its account is not proof of the tested platform account. |
| Mutation and recovery | Identify fixture owner, allowed mutations, spend/time limits, recovery state location and cleanup responsibility. Confirm prior cleanup and shared feature-flag restoration before another mutating run. |

If a lane resolves to a different or unresolved deployment, correct its settings
before running it. A narrower diagnostic can validate individual prerequisites;
its pass does not establish full-pack or release acceptance. Evaluator setup
installs base identity/profile/observer access and three GitHub secrets; it does
not deploy ADP or complete the other lanes' readiness checks.

### Proposed unified targeting contract

Future work should provide one versioned private target definition, a shared
resolver and preflight that validates every selected lane's effective account,
region, resource environment, endpoints, identities, fixtures and cleanup boundary
before mutation. It should reject inconsistent targets and emit the resolved
target identity with run evidence. Component revision/digest attestation should
cover frontend, workers and domains as well as the gateway. These capabilities
are **proposed**, not current workflow inputs or guarantees.

## Results and cleanup

Pytest live suites record their selected case IDs before execution. The report
compares actual JUnit testcase records against that inventory. Missing reports,
zero selected/executed tests, duplicate results, required skips and failures cannot
pass. Opposite-feature deselection remains explicit, and a disabled Chat target
cannot satisfy the scheduled enabled-conversation gate. Offline passes and xfails
do not establish deployed capability acceptance.

Both coordinators require usable gateway smoke credentials. A missing refresh
token fails the coordinated run, even though legacy standalone smoke callers
may still opt into the existing optional-token behavior.

The combined report requires every selected workflow lane to succeed. It also
requires matching verified gateway revisions before and after the live run. The
CLI lane separately checks its initial and EC2 revisions. These are gateway
revision checks, not proof that every frontend/worker/domain digest is identical;
release qualification still needs its component-specific deployment receipts.

Live leaves share a `cli-live-eval-<target>` lock. The coordinator has a
different lock so it does not deadlock reusable children. Deploy checks and
scheduled coordinators serialize with each other. Independent offline chains
continue after failures. Later mutating live lanes require a successful prior
lane or explicit verified CLI cleanup; unknown cleanup blocks continuation.
The gateway API teardown now fails if its recorded cleanup cannot complete.
Inspect the owned fixture after an interrupted API run before retrying; its
in-process cleanup is not the EC2 harness's durable recovery mechanism.

Existing deployments retain their own deployment locks. They do not acquire a
new global lease in this change; a concurrent deployment is detected as gateway
revision drift and requires a rerun. This also does not guarantee detection of a
change that moves away from and back to the same gateway revision between checks.

No run automatically files a new failure issue. Use the combined Actions verdict
and suite artifacts; existing credential-readiness checks now consume the sole
scheduled coordinator's latest result and preserve their freshness requirement.

## Operation

The following commands use the default `dev` target:

```bash
gh workflow run adp-regression.yml --ref main -f profile=daily
gh workflow run adp-regression.yml --ref main -f profile=weekly
gh workflow run adp-deploy-check.yml --ref main
gh run list --workflow adp-regression.yml --event schedule --limit 5
```

For an explicit configured target, set `GITHUB_REPOSITORY`, `TARGET_ENVIRONMENT`
and `DEPLOYED_REVISION` from the reviewed private target record. Set
`REGRESSION_MODULES` to module IDs/names, `all`, or an empty string for the profile:

```bash
gh workflow run adp-regression.yml --repo "$GITHUB_REPOSITORY" --ref main \
  -f target_environment="$TARGET_ENVIRONMENT" \
  -f expected_revision="$DEPLOYED_REVISION" \
  -f profile=daily -f modules="$REGRESSION_MODULES"
```

Dispatch from `main`; `expected_revision` refers to the deployed gateway, not
the workflow checkout. See [module selection](module-selection.md) for IDs and
selection semantics: prerequisite checks and shared suites can cover more than
the requested module. Selecting a target does not bypass the limitations or
operator preflight above.

Manual leaf dispatches remain available for diagnosis and retain the shared live
lock. Paid manual-only qualifications keep their own reviewed configurations.
An unrelated non-dev or cross-account manual deployment must not be interpreted
as acceptance by the dev deployment check; automated completion triggers accept
only ordinary main push deployments. Use the target's existing qualification path
for other deployments.

Rollout is complete only after private prerequisites are configured and observed
daily/weekly runs execute their expected cases and clean up successfully. Record
runtime and cost from those runs; fast historical runs that skipped coverage are
not a useful performance baseline. Required feature, fixture and implementation
gaps remain coverage debt even when a narrower diagnostic passes.
