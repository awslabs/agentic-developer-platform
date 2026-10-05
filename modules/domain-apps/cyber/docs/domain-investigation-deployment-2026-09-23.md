# Cyber domain investigation deployment — 23 September 2026

The agent-directed domain investigation runtime is deployed in Embark1,
account `879318057152`, `us-east-1`, cluster `adp-dev-eks-cluster`.
The browser broker is healthy with two replicas. New agent workers use the
validated immutable image, and the image-prepull DaemonSet is ready on all
13 current nodes. The warm pool remains at zero replicas.

## Release and preservation

- Implementation: [PR #5808](https://github.com/aws-e/adp/pull/5808).
- DNS rotation correction: [PR #5810](https://github.com/aws-e/adp/pull/5810).
- Runtime source: `fdddb828387af49663caf22920be2638f3caa1a0` on
  `release/cyber-preserve-worker-20260923`.
- Immutable image: `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:7cd991c4b1498295bfa331da8deb367d4a02bba0e12b0075d73b61c7f9ced08f`.
- [ARC build 35870642701](https://github.com/aws-e/adp/actions/runs/35870642701)
  used `arc-runner-org` and CodeBuild `adp-dev-agent-runtime:6796a349-f6cf-41ad-8423-aef19852e327`.

A separate AI-DLC handoff repair was already deployed in worker source
`005dd2c4384c64c77803d91c34b87ffd85917954` when this session resumed. The release
retains that source and changes only the cyber agent subtree, which matches the
reviewed implementation. It does not replace that repair with an older main build.

Reviewed saved Terraform plans updated only the browser broker Deployment and
Service: image, ClientIP session affinity, and protection from voluntary node
consolidation. Subsequent worker, warm-pool and prepull JSON patches tested the
current resource version and image, and verified that only image fields changed.
The ScaledJob retains its gradual rollout; existing Jobs were not restarted.
The protected-worker migration hold and activation flags remain unchanged.
The final integration check preserved GitHub credentials, installation mappings
and broker settings. No publicly invocable Lambda was created.

## Live acceptance

A real Bedrock `us.anthropic.claude-sonnet-4-6` model chose actions through the maintained
investigation commands in a temporary pod using the final worker image and
`agent-scaledjob-sa`. The commands used the deployed broker and actual AgentCore
browser. The reasoning harness ran locally; this was not a UI/GitHub ingress run.

The model independently selected the verification link, observed the form while
retaining session storage, selected the operator disclosure, and finished after
3 model turns with 3 observations and
3 evidence reviews. The known synthetic fixture was assessed as
`no_adverse_behavior_observed`. No form was submitted. All 10 case files
passed integrity verification. The explicit stopping reason and hypothesis
updates are retained in the case and report.

Legacy capture of `https://example.com/` also passed, with
6 files verified. The worker's direct AgentCore access
returned `AccessDeniedException`, while private destination requests
were refused by the broker. Both successful sessions, and the earlier failed
session, were independently confirmed `TERMINATED`. The temporary Job and
ConfigMap were removed. The three-image HTML report was visually inspected at
1440 and 390 pixels with no horizontal overflow.

The first managed run exposed public DNS rotation between pages. The guard now
uses only addresses present in both the original approved set and the freshly
validated answer; it never expands the approved set. Private answers and complete
address replacement remain refused. That failed case and its cleanup evidence
are retained separately. The final regression suite passed 256 URL tests plus
six IAM boundary tests, and ARC checks passed.

## Evidence and use

The final case archive was uploaded by the worker and read back byte-for-byte:

- `s3://adp-dev-url-analysis-evidence-v2-879318057152/tenant=adp-default/issue=0/run=cyber-investigation-20260923-v2/research-cases.zip`
- SHA-256: `b0f252e2cd370553bf5506ce20fc1c66c900c7c06272273c6080fe91543fa247`

It contains HTML/Markdown reports, case and manifest JSON, screenshots, DOM
snapshots, observed-indicator CSV, decisions and evidence references. The
[machine-readable deployment record](domain-investigation-deployment-2026-09-23.json)
contains the workload, session and validation summaries. Private Terraform state
and plan snapshots are excluded from the repository.

Give the hosted `malware-analysis-agent` a seed URL and a research question;
for example, ask who operates an account-verification flow, what information it
requests, and which observations support its claimed affiliation. On an already
connected GitHub repository, the configured mention is `@agent-malware-analysis-agent`.
The default scope is the seed hostname and its subdomains; external navigation
requires an explicit research request.

The top-level `adp` CLI was not changed and does not have an `adp cyber` command.
The new investigation CLI runs inside the hosted worker. See
[domain investigations](domain-investigations.md) for that interface.
Normal UI/GitHub invocation and report delivery still need their own acceptance
run. This controlled case demonstrates adaptive browsing and evidence handling;
it does not measure threat-detection accuracy, Intelix integration or CAPE detonation.
