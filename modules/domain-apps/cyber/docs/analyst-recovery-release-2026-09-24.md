# Cyber analyst recovery release — 24 September 2026

This dev release gives the analyst one assessment across live observations,
retrieved archives and sourced context, with `clean`, `suspicious`, `malicious` and
`inconclusive` as the schema for new judgments. Unfinished collection and model
execution are pending, not an inconclusive verdict. It simplifies navigation and
reason records, separates screenshot capture, enables provenance-preserving
same-run evidence reuse and broadens historical archive discovery.

## Release identity and scope

- Account `879318057152`, profile `embark1`, region `us-east-1`.
- Implementation [#5917](https://github.com/aws-e/adp/pull/5917), merged as
  `a107e3cfb68cd75d8c205ad566ad71cc62dd857b`.
- Hybrid release source `fd9561392e163b26875aa9afdc1276ad15217594` replaces only the
  cyber agent subtree of the preceding release `5285858f5447578d5c834da2a0ddd1f8e0faef5a`.
  The preserved worker base is `de3227b36c7956c181cfd486876c90c19bec354e`.
- Successful CodeBuild `adp-dev-agent-runtime:cdded5ea-0a3f-4064-8443-2c903671618e`.
  Source packaging used committed `git archive`; `PUBLISH_LATEST=false`.
- Initial worker and current broker image:
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:073918cf6405bae0158957588eb6acb8c6f3485d04e08fb091659066827b4e24`.
- Rollback image preceding the initial release:
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:bfac5d37d711bdd8a96b0a3c7fe3b542916dbd52afe4d7cc5d46798aeb168d21`.

The whole-module webhook/security migration hold remains. Fresh saved Terraform
plans changed only the worker archive-read policy and broker image:

| Resource | Change | Plan SHA-256 |
|---|---|---|
| `module.cyber.aws_iam_role_policy.worker_common_crawl[0]` | Adds WARC reads for June, May and April partitions (`2026-25`, `2026-21`, `2026-17`) | `50004667962e5a737c0280485c1ae3b487430143885337a6a31a3c1198cb595d` |
| `module.cyber.kubernetes_deployment.url_analysis_browser_broker` | Image only | `26e8568ff2ca474c359c09b95316c83c995fc3a15a0e25c8d460a9f6649bd16a` |

Both brokers reported zero active sessions before rollout, and AWS listed none.
Resource-version and old-image guards protect the ScaledJob, warm-pool and prepull
patches. The ScaledJob gets the same six configured crawl partitions; the warm
pool remains at zero. Private-network, credential/form, download and challenge
protections remain. No operator evidence-bucket deny was weakened or bypassed.

## Validation

366 Python tests, 9 IAM boundary tests, three mocked Terraform plans, Terraform
validation/formatting and scoped Ruff checks passed. All applicable implementation
PR CI checks passed, including URL browser fixtures, worker IAM/deployment safety,
kernel isolation, production-image probes, webhook unit tests and package checks.

The hosted canary used the deployed image, broker and normal worker identity:

- Exact May lookup `4c036776-e6d4-4e41-905f-b5a3457ac48c` succeeded after
  194.096 seconds of observed queue wait and 4.085 seconds of execution wait.
- It retrieved and extracted a 946-byte WARC record from `CC-MAIN-2026-21`,
  exercising the newly scoped historical read permission.
- An archive-only `clean` assessment passed. New browser evidence returned to
  pending; initial text capture did not request a screenshot or frames.
- Explicit screenshot capture succeeded, followed by public navigation without a
  prior review record. Three observations were imported into a same-run case with
  provenance, and the imported-source assessment passed.
- RDAP of a subdomain succeeded using its registrable parent.
- The worker uploaded/read back and verified 19 files. AWS independently confirmed
  Browser session `01M3A3VR8EZDN3D1DMX0040YS6` was `TERMINATED`.
- The first canary attempt reached archive retrieval but stopped on a fixture call
  to `assess_case` without a stopping reason. Using the documented `finish` API
  corrected the fixture; no product or IAM change was needed.

Acceptance is in the evidence bucket at
`tenant=adp-default/issue=0/run=analyst-recovery-20260924/canary/acceptance.json`.
This is scripted functional verification, not model detection accuracy.

## Controlled assessment replay

The final replay used `global.anthropic.claude-opus-5`, the same inference settings
and byte-identical verified evidence in both arms. It compared the previous URL
skill/schema with the revised skill/schema, without browser actions, memory,
previous verdicts, analyst reviews, hypotheses or benchmark labels. Arm order
alternated. Extracted archive content, inert DOM, screenshots and sources were
included. Raw responses and invocation identity are recorded separately; a model's
self-description is not used as its identity.

| Preserved case | Baseline raw verdict | Revised verdict | Reference/structure validation |
|---|---|---|---|
| Public control from #5904 | `no_specific_concern` | `clean` (high confidence) | Baseline cited an unknown observation; revised passed |
| Archive-only registry case from #5904 | `no_specific_concern` | `clean` (medium confidence) | Both passed |
| Public framework case from #5908 | `no_specific_concern` | `clean` (high confidence) | Baseline cited an unknown observation; revised passed |
| Suspected impersonation retry from #5912 | `suspicious` | `malicious` (medium confidence) | Both passed |

The old package also made a benign archive-only judgment. Earlier live-run
inconclusive results therefore cannot be explained solely by model capability or
this skill text. Tool interactions and the surrounding runtime context matter.

These are single samples measuring prompt/contract sensitivity, not accuracy.
Validation does not certify factual claims. In the suspect-page replay the revised
arm asserted non-affiliation without independent ownership evidence and inferred
login intent; neither arm established a credential submission endpoint. Both arms
speculated about script-generated UI rather than establishing the cause of the
screenshot/DOM mismatch. The stronger verdict must not be presented as verified
credential theft or a proven accuracy improvement.

The authorized AWS worker verified and stored the final result at
`tenant=adp-default/issue=0/run=analyst-recovery-20260924/replay-results-v5.json` in
the configured evidence bucket. Package hashes:

- Baseline: `20f55733fa5f7fd5dfc31f2efce50159bc80d1f283663286d82bdda333b89ee9`.
- Revised: `ff127bff0dd42a53e7dbcecd252f288cb3bc252025670fe3c2969bad00533eb9`.

## Browser transport comparison

A separate read-only experiment ran under the broker identity on public controls.
On the first control, guarded and native Browser produced identical text and
screenshot hashes. On the registry control, native Browser captured text and a
screenshot in about four seconds including startup. Guarded navigation returned
HTTP 200, but text extraction stalled for 153.57 seconds and required operator
termination of that specific Browser session. The comparison process was then
stopped; a third-control session had begun without a result and was also stopped.
All five experiment sessions were independently verified terminated through AWS.

This is evidence of a guarded collection problem, not validation of equivalent
network protections in the native path. The production guard remains. Metrics,
not raw screenshots, were retained for this experiment. The revised collector
retains a checkpoint before DOM capture and allows assessment from other sources;
it does not claim to eliminate the underlying guarded DOM stall.


## Fresh GitHub runs

Five neutral issues cover the same 25 unique May URLs:
[#5919](https://github.com/aws-e/adp/issues/5919),
[#5920](https://github.com/aws-e/adp/issues/5920),
[#5921](https://github.com/aws-e/adp/issues/5921),
[#5922](https://github.com/aws-e/adp/issues/5922),
[#5923](https://github.com/aws-e/adp/issues/5923).
All five workers started on the new image. Runtime inspection confirmed the four
new schema labels, all six archive partitions and skill SHA-256
`e35a64fdfbc325a21b5e55417619ff8e6646ab18dd5b0a4939e34915bed73079`.
The first worker's session initialization records `global.anthropic.claude-opus-5`.

The issue bodies contain URLs and investigation/publication instructions, without
historical verdicts or expected labels. They prohibit using older investigations
or repository memory as evidence. The preserved platform still injects recent
memory summaries, so these live runs must not be described as fully isolated blind
benchmarks. The controlled replay above excludes that memory.

All 25 submitted URLs received model assessments. Superseded pending cases and a
companion parent-domain case are excluded from these totals.

| Run | Clean | Suspicious | Malicious | Inconclusive |
|---|---:|---:|---:|---:|
| #5919 | 4 | 1 | 0 | 0 |
| #5920 | 1 | 3 | 0 | 1 |
| #5921 | 1 | 1 | 0 | 3 |
| #5922 | 1 | 1 | 0 | 3 |
| #5923 | 2 | 2 | 0 | 1 |
| **Total** | **9** | **8** | **0** | **8** |

The earlier September pass reported five `no_specific_concern` and 20
`inconclusive`; May reported nine clean, three suspicious and 13 malicious.
The [complete 25-URL comparison](https://github.com/aws-e/adp/pull/5924#issuecomment-5818550683)
preserves the original labels and links to each report. May is not ground truth:
several original requests included incident narratives or expected labels, and
the endpoints were revisited months later. The 13 May-malicious URLs now yield
eight suspicious and five inconclusive. Fewer inconclusive results do not
establish greater accuracy.

The new runs did use archives and independently sourced context to reach
decisions despite missing live content. They also retained reasoning limitations:
unchanged versioned script filenames do not prove unchanged script bytes;
registration holds do not establish their cause; and a timeout alone does not
locate a network failure. One report claims an independent summarising fetch but
cites an empty archive result. The [publication follow-up](https://github.com/aws-e/adp/issues/5920#issuecomment-5818622775)
confirmed no original retrieval record was preserved and added a provenance note
without changing the original case or verdict. Domain-authenticity records remain
checkable; the asserted page content does not. Structural reference validation
does not establish that a cited source supports the claim.

Case reports and assets remain in AWS. Workers verified artifact readback and
signed GET access; subsequent publication tasks add clickable indexes to the same
issues without repeating the investigations. Temporary worker credentials cap
the signed links at about one hour despite a requested seven-day signature.
The issue indexes retain durable S3 paths, and the report-assets ZIPs contain
relative assets that a directly opened signed HTML object cannot authenticate.

At 17:00 UTC the operator enumerated all seven pages of the AWS Browser session
API. All 31 sessions created since the runs began at 16:28 UTC were terminated;
none of the 344 listed sessions was active. This includes one case-complete
orphan from #5919 that the operator found READY, stopped at 16:52:32 UTC and
independently verified terminated. Original case records retain their reported
unknown cleanup states; the subsequent verification does not reconstruct missing
session IDs or pretend cleanup succeeded immediately. No active investigation
session was terminated.

## Rejected-draft recovery correction

The live run in #5923 exposed a report persistence defect: recovery retained raw
source-only findings from a rejected assessment, omitting optional fields that the
report renderer expected. A later close could fail with `KeyError: evidence_ids`.
The agent recovered by creating a replacement case with explicit citation fields.

[#5931](https://github.com/aws-e/adp/pull/5931) normalizes retained findings and
checks their source references before preserving them. Three synthetic regressions
exercise absent references, unknown sources and unknown observations through
rejection, close, report/manifest verification and correction in the same case.
Those tests, 18 existing workflow/evaluation tests and scoped Ruff checks passed.

The correction's hybrid source is
`2ac9af8aa209674ff77d0211f69a9e3768a14f0d`, changing only the recovery function and
its tests from the original release. CodeBuild
`adp-dev-agent-runtime:b443baf4-ca35-49d0-84c2-67adbbfdbe28` succeeded with committed
archive input and `PUBLISH_LATEST=false`. Its image is
`879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:9b07180e2304cd075bfb5d3309f010b8a458f2973a7cf034c0ef82a79b3900f4`.

A normal AWS worker running that image corrected all three synthetic rejected
drafts and uploaded/read back/hash checked 15 artifacts; all 15 signed GETs were
also verified from the worker. No Browser sessions were started. Acceptance is
stored at `tenant=adp-default/issue=0/run=assessment-recovery-20260924/canary/acceptance.json`.
The temporary canary Job and ConfigMap were removed after successful completion.

All applicable CI checks passed and #5931 merged as
`fce54b09443e724fbe52ac74b3384de6713ca605`. The ScaledJob, zero-replica warm pool
and image-prepull DaemonSet were patched with old-image and resource-version
guards. The broker continues on the original release image because its code did
not change; no Terraform apply was needed for this worker-only correction. The
five comparison workers retain their original image identity; this correction
changes no model, skill, assessment schema or browser transport.

At the final cache check (17:09 UTC), all 12 currently scheduled prepull pods were
updated and ready; node count changed during cluster autoscaling. The broker
remained 2/2 ready. Unrelated historical workers were not restarted.
