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

## Configuration before activation

Configure the existing protected environments and main-branch restrictions. Do
not put real target identifiers or credentials in public examples.

| Environment | Required configuration |
| --- | --- |
| `dev` | Existing CLI evaluator OIDC role secret, `CLI_UPLIFT_EVAL_BINDINGS_JSON`, `CLI_UPLIFT_EVAL_GATEWAY_CATALOG`, and the fixture references required by the selected CLI scope. Initial/final revision snapshots use the same private bindings. |
| `adp-checks-dev` | `ADP_CHECKS_ROLE_ARN`, authorized smoke credentials, gateway live bindings below and the existing integration fixtures. The checks role must be authorized for the exact test credential references. |
| `adp-browser-checks-dev` | Scoped `ADP_CHECKS_ROLE_ARN`, browser fixture credentials and a reachable deployment. Daily Chat requires chat actually enabled; setting an expected flag does not enable the product. |
| `adp-deploy-dev` | The reviewed `ADP_DEPLOY_ROLE_ARN` and database configuration required by onboarding/budget/adversarial workflows. A missing role must be provisioned through the existing reviewed automation infrastructure; never substitute an ambient or administrator identity. |

`GATEWAY_LIVE_TEST_BINDINGS_JSON` is a protected checks-environment secret holding
identifiers, not credential values. Its shape is:

```json
{
  "account_id": "000000000101",
  "gateway_url": "https://gateway.example.com",
  "api_gateway_url": "https://api.example.com/stage",
  "m2m_secret_name": "adp/dev/gateway/regression-m2m",
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

The coordinator does not provision a regression deployment, enable Chat, create
the required persistent fixture tenants, or grant missing IAM permissions. Existing
live harnesses do create temporary identities, data and compute. In particular,
onboarding temporarily changes the shared organization-assignment enforcement
flag and must restore it before budget tests proceed. Preflight failures
are actionable configuration failures, not passing coverage. A dedicated enabled
regression target and a separate approved disabled-feature target are still
needed to establish both feature modes. This initial coordinator targets dev.

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

Live leaves share the existing `cli-live-eval-dev` lock. The coordinator has a
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

```bash
gh workflow run adp-regression.yml --ref main -f profile=daily
gh workflow run adp-regression.yml --ref main -f profile=weekly
gh workflow run adp-deploy-check.yml --ref main
gh run list --workflow adp-regression.yml --event schedule --limit 5
```

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
