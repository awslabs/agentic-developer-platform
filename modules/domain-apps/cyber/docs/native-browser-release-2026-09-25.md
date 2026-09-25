# Direct Browser release — 25 September 2026

The dev cyber worker now connects directly to AgentCore Browser through its
signed CDP endpoint and Playwright. The custom browser broker deployment has
zero replicas. A worker-local session process preserves browser state across CLI
calls; it has no separate service or credential identity. Common Crawl and the
analyst's assessment workflow remain available in the application.

## Release identity

- AWS account `879318057152`, profile `embark1`, region `us-east-1`.
- Implementation: [PR #6078](https://github.com/aws-e/adp/pull/6078), merged as
  `1b75bce5457112999956eb9a459d7f091927e0cb`.
- Runtime source: `2a2b1200c008461e66675a38afc798cca097f0f8` (identity correction in
  [PR #6083](https://github.com/aws-e/adp/pull/6083)).
- CodeBuild: `adp-dev-agent-runtime:c70e0944-54de-4a83-bce0-dabdaf700eaf`.
- Deployed worker:
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:f90e802c40b20edffd7ae7ccaa7ba6af1072158e7ae349535d6a1728e2bfa63d`.
- Preserved protected-worker base:
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:1cb3550ee64d72b3b5261ccba7c874378a309d71ca277cb985dc4c911edce51f`.
- Release overrides: [`../releases/native-browser-dev.json`](../releases/native-browser-dev.json).
  Apply alongside the retained protected platform configuration.

Native mode removes the custom offline/replay transport, per-request
interception, service-worker/WebSocket blocking and popup target rewriting.
Action timeouts, session cleanup, evidence provenance and the protected worker
baseline remain. Native subprocesses strip `NODE_OPTIONS` to prevent injected
instrumentation from corrupting Playwright's private Node protocol.

## Scoped deployment

The whole-module webhook infrastructure hold remains. Saved Terraform plans
and guarded Kubernetes patches changed only the following:

1. The protected worker IAM policy and boundary gained five regional Browser
   actions: start, get, list, stop and automation-stream connection. IAM simulation
   allowed Browser start/CDP and continued to explicitly deny generic AgentCore
   runtime invocation, direct model invocation, S3 reads and secrets reads.
2. Both gateway worker-image allowlists retain the protected base digest and add
   the new worker digest. The gateway rollout completed with eight ready replicas;
   effective allowlists and authority activation were verified inside a pod.
3. The ScaledJob, warm-pool template and image-prepull template use the immutable
   worker image. The ScaledJob uses `URL_ANALYSIS_BROWSER_MODE=native` and has no
   broker URL. Warm-pool replicas remain zero. Admission was already paused by a
   separate platform migration and was later unpaused independently; this release
   did not change the pause setting.
4. Both old broker replicas reported zero active sessions before a scoped plan
   scaled the deployment from two replicas to zero. Compatibility infrastructure
   remains available for rollback.
5. The first demo trigger exposed an existing missing webhook admission grant.
   A separate reviewed saved plan created only
   `aws_iam_role_policy.lambda_work_claim_admission[0]`, allowing
   `execute-api:Invoke` on
   `arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/POST/internal/v1/agent/work/admit`.
   IAM simulation then returned `allowed`, and the normal GitHub trigger admitted
   a worker. No admission checks were disabled.

## Verification

Local checks passed: 385 URL-analysis/browser-boundary tests, 144 worker IAM and
deployment-safety tests, 15 worker Terraform tests, four cyber Terraform tests,
five hosted-evaluation tests and five native Chromium tests. Applicable PR CI
passed, including hardening and production-image checks.

The final hosted canary ran the corrected image under the protected worker
service account **after** applying the normal `configure_task_credentials`
transition. The caller kept its task credential configuration throughout; Browser
startup, CDP signing and emergency cleanup selected platform identity only within
the fixed Browser implementation. The task-session policy was unchanged.

Collection results:

| Public control | Browser startup and text | Text characters | Screenshot action |
|---|---:|---:|---:|
| Example | 3.44 s | 129 | 0.13 s |
| IANA | 3.80 s | 1,238 | 0.32 s |
| Sophos | 5.38 s | 6,928 | 5.67 s |

IANA also navigated to its reserved-domains page in the same session in 0.44 s.
Sophos returned HTTP 200 and a 441,937-byte screenshot. Its text capture reported
truncation, which remains a coverage limitation rather than a verdict veto.
The successful canary Job was `cyber-native-browser-identity-final-20260925` in
`adp-agents`. All three sessions were independently verified `TERMINATED`, and
repeated close was idempotent:

- `01M3BW76WWGRAVJA9V1K8M0TGH`
- `01M3BW7AR9R9M4T9FPY88ERR7S`
- `01M3BW7FEA6CGC4HMAA0NKX175`

An intermediate canary was evicted during node consolidation. Retrying with the
normal worker's `karpenter.sh/do-not-disrupt=true` annotation succeeded; all
intermediate Browser sessions were also verified terminated.

The previous guarded experiment stalled for 153.57 seconds in DOM extraction
after navigation succeeded. These direct-mode canaries demonstrate working
collection on the sampled pages, not a universal speedup or detection-accuracy
improvement. AgentCore session isolation and expiry do not establish equivalence
to the removed per-request private-address filtering. That equivalence was not
tested or claimed. No evidence-bucket policy was weakened or bypassed.

## GitHub demo

[Issue #6080](https://github.com/aws-e/adp/issues/6080) contains three inputs and
independent investigation instructions, without expected verdicts or previous
results. The successful
[retry trigger](https://github.com/aws-e/adp/issues/6080#issuecomment-5829467615)
started [check run 108003595605](https://github.com/aws-e/adp/runs/108003595605)
on pod `agent-scaledjob-bqjdv-fbl2q` with the initial native-browser image
(`sha256:f14998af44cc77df24365371675ffe7fef54878d7a254c83e65a9dcbcbfdbabe`).
Correlation: `894a1979-91a7-4ce4-8a90-50f0e5415cb5`.

The preserved platform still injects recent repository/agent memory; the issue
instructs the analyst not to use it as evidence. This is therefore a live workflow
demo, not an isolated blind benchmark. The runtime reported
`global.anthropic.claude-sonnet-5`; no model-accuracy comparison with May is claimed.

That first live run exposed an identity integration error: the task shell uses a
restricted customer STS credential process, which denies Browser. The original
canary had not applied that credential transition. PR #6083 corrects identity
selection inside the Browser process and its emergency cleanup client. Tests
exercise both real AgentCore SDK credential chains with conflicting task
credentials and verify that the caller environment remains unchanged.

An intermediate corrected-image canary omitted the worker pod label and therefore
hit the namespace default-deny egress policy during DNS resolution. Its managed
sessions were cleaned up. The final canary carries the standard worker label and
passes under the existing DNS/HTTPS policy; no network policy was modified.

The corrected image is deployed to the ScaledJob, warm-pool and prepull templates.
The gateway rollout completed with seven ready updated replicas, and the new
digest was verified in both effective worker-image allowlists. A fresh demo was
triggered at [issue #6091](https://github.com/aws-e/adp/issues/6091), with the same
three input URLs and no prior results.

[Run 108008726679](https://github.com/aws-e/adp/runs/108008726679), correlation
`22ed0c3e-34d6-433b-b0d0-4eea45f3f85a`, ran on
`agent-scaledjob-q672x-dq46m` with the corrected digest. It successfully collected
all three targets: two observations for Sophos, one for IANA and four for the
Webflow target, including screenshot capture and follow-up navigation within the
same session. The completed case records contain these model assessments:

| Target | Agent verdict | Agent confidence |
|---|---|---|
| Sophos | clean | high |
| IANA | clean | high |
| sso-auths-bitmart-sso.webflow.io | malicious | high |

These are the agent’s judgments, not independently established detection accuracy.
All three session IDs were independently verified `TERMINATED` through AWS:
`01M3BWHA2QN183FKZX91N314P5`, `01M3BWMY6SH88XY3ZG9QG2A01K` and
`01M3BWPVDMWFNFJXE6XYCVQE4D`.

Common Crawl remained unavailable because the protected task-session policy
denies Athena. The agent initially reported successful S3 upload/readback, then corrected
that claim: local `verify` checks file hashes, and direct S3 writes are blocked
by an explicit IAM deny. Standalone presigned links return HTTP 403. Artifact
delivery is therefore unverified and blocked through the attempted direct path. The operator did not fetch those URLs or read the evidence
bucket. See the linked issue for the published reports and subsequent delivery
status. No bucket policy was changed.
Browser-canary success does not establish
that archive access, artifact publication or the full analyst workflow succeeds
under the separately migrated protected-worker permissions.
