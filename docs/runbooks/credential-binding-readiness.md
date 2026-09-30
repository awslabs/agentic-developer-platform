# Credential-binding readiness evidence

The enforcement workflow calls `platform/scripts/flip-gate-check.sh`. The
preflight is read-only. Exit zero means its evidence checks passed; it neither
deploys nor authorizes an enforcement flip. Follow #3186 for the actual flip.

```bash
AWS_PROFILE=example-profile bash platform/scripts/flip-gate-check.sh \
  --environment dev --aws-region us-east-1 --window-days 7 --repo aws-e/adp
```

The gateway emits four numeric EMF metrics in `BedrockGateway`, dimension
`Environment`: `CredentialAuthorizationChecked`,
`CredentialAuthorizationFromRegistry`, `CredentialAuthorizationDrift` and
`CredentialAuthorizationFallback`. Every completed binding decision emits one
coherent sample, including zero drift/fallback values. Refusals are observed
before raising the existing exception. No identity or credential is in the
new metric payload; authorization behavior is unchanged.

After an approved deployment, verify that the log collector extracts these EMF
samples into CloudWatch for the correct environment. Source code and stdout
alone are not evidence of successful ingestion. A historical absence of drift
metrics does not establish a soak. Accumulate at least seven complete UTC days
with positive credential-call observations on each day. The gate requires all
four daily series, positive checked counts, registry counts equal to checked
counts, and zero drift and fallback. It also rejects drift or incomplete
ingestion in today's bucket when present. Quiet days, AWS errors, malformed
data and partial series block readiness.

The gate checks the latest scheduled main-branch adversarial nightly (success,
completed, at most 36 hours old), plus the sandbox credential-enable parameter.
That nightly currently targets dev; it cannot certify staging or production.
These checks do not independently authenticate the nightly's source artifacts.
Review its run, transcript, audit rows and fixture identities before accepting
it. The assertion script now rejects missing transcripts/audits, unexercised
credential boundaries, skipped-only suites and empty suites. Negative controls
require a recorded unauthorized victim read; missing evidence cannot pass by
inverting a failed assertion.

## Outstanding sandbox setup observed on 2026-09-20

- Run `35431762403` failed before dispatch because the assertion step did not
  supply `GH_TOKEN` to `gh issue create`.
- The configured cross-repository target `aws-e/adp-security-test-repo` returned
  404 to the current operator identity. That does not distinguish absence from
  inaccessible permissions. Verify the repository, installed sandbox app,
  labels, tenant and attacker/victim fixtures before selecting a token.
- Do not substitute the workflow's repository-scoped token or an arbitrary app
  identity: successful issue creation alone does not prove the intended root.
  Provision a credential scoped to the verified sandbox identity and repository,
  then pass it only to the assertion step. No credential is provisioned here.
- Validate the audit endpoint from the runner. Its existing HTML/error fallback
  returns no entries; the repaired assertion treats that as missing evidence.
  Verify run/tenant/attacker attribution and a real denial before certification.
- The failure-report job also failed because `adversarial-e2e-failure` was not
  present. Repair its label configuration during sandbox setup.

No nightly or model invocation was dispatched to validate this code. Local
regressions use command/HTTP doubles. Model spend, deployments, IAM changes and
the enforcement workflow retain their operator gates.
