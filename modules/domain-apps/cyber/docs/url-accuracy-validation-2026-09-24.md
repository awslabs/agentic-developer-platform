# URL evidence changes: isolated AWS acceptance

The updated source was tested in temporary broker and worker Jobs in Embark1,
account `879318057152`, `us-east-1`. The production broker and workers were not
updated. Test pods had distinct service selectors and scoped network policies.

The existing runtime image was
`sha256:d939fd55c4575dd367ceebc8b2a7a89754477b0d9040d9f6545cf9ffd594995c`.
The mounted source bundle, including the acceptance harness, had SHA-256
`3fb4d472451f90802d2bcb601a5b799c40e3f7e1764262b8819084ac34878227`.

## Results

| Case | Collection | Assessment |
|---|---|---|
| Previously tested AR24 example (PhishTank 9530788) | HTTP 200; complete capture; full 1,841-character inline handler | `malicious`, accepted with verified evidence after one assessment correction |
| Previously unresolvable example (PhishTank 9530191) | `unavailable`, zero observations | Inconclusive collection result; **no model invocation** |
| Reviewed example.com documentation control | HTTP 200; complete capture | `no_adverse_behavior_observed`, accepted without correction |

The AR24 handler was previously cut at 1,200 characters and the previous case
remained inconclusive. The fresh capture retained the full handler. The model
assessed the captured page evidence without receiving its PhishTank label or
previous verdict. No credentials were entered and no form was submitted.

The two browser sessions were independently confirmed `TERMINATED` through
AgentCore `GetBrowserSession`. The capture archive passed S3 byte-for-byte
readback verification. Separate assessed reports and integrity verification are
recorded under the prefix below.

All real-site inputs, captures, scripts, screenshots and reports remain in S3:

`s3://adp-dev-url-analysis-evidence-v2-879318057152/tenant=adp-default/issue=0/run=cyber-accuracy-20260924/`

- `summary.json`: capture outcomes, model results and development-set metrics.
- `development-manifest.json`: three pinned source snapshots and label provenance.
- `assessments/`: assessed case JSON, HTML/Markdown reports and evidence manifests.
- `validation.json`: report verification and runtime provenance.
- `session-cleanup-verification.json`: independent session-state verification.
- `captures.zip`: original captured cases, before snapshot assessment.

## Scope and limitations

Local validation passed 280 URL tests, including real Chromium synthetic fixtures,
and nine cyber integration/boundary checks. These cover complete versus truncated
handlers, unrelated coverage gaps, legitimate authentication, failed navigation,
unknown references, integrity changes, deterministic DNS handling, and preserving
browser context after an invalid assessment.

This is a small acceptance run of fresh capture followed by blind snapshot
assessment. It verifies the specific regressions; it is not a representative
accuracy estimate, UI/GitHub invocation test, deployment, or verification of
VirusTotal connectivity. The initial S3 development set has three examples, not
220 independently reviewed examples or a completed holdout set. The larger
curated corpus and evaluation remain necessary before claiming an overall
accuracy improvement.
