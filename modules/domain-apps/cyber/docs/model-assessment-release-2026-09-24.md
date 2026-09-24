# Model-owned cyber assessment release — 24 September 2026

This scoped dev release lets the model assess Common Crawl content, live browser
observations and attributed context together. It removes semantic verdict vetoes
and defaults navigation to relevant observed public links across hostnames.
It also adds model-selected Common Crawl WARC reads and inert page extraction,
following the retrieval pattern reviewed in `aws-e/cip`.

## Release identity

- Account `879318057152`, profile `embark1`, region `us-east-1`.
- Implementation [#5898](https://github.com/aws-e/adp/pull/5898), merged as
  `eafee7c92963c8dcf02b7026ee417a55f02f52c7`.
- Release source `5285858f5447578d5c834da2a0ddd1f8e0faef5a` replaces only the
  cyber agent subtree of the previous release `bba34f36582af8fa2a901eeb7b86b7f572272c44`.
  The deployed worker base remains `de3227b36c7956c181cfd486876c90c19bec354e`.
- Successful CodeBuild `adp-dev-agent-runtime:df8c9beb-8dd3-478e-8907-0a12cd703dd9`.
  Source packaging used committed `git archive` content; `PUBLISH_LATEST=false`.
- Worker and broker image:
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:bfac5d37d711bdd8a96b0a3c7fe3b542916dbd52afe4d7cc5d46798aeb168d21`.

## Scoped rollout

Fresh saved plans changed exactly one resource each:

1. `module.cyber.aws_iam_role_policy.worker_common_crawl[0]` adds only
   `s3:GetObject` on WARC files in the three configured crawl partitions.
   Plan SHA-256: `7d44f4926041797ce73a7490b50ca467eafbdbf7c1db486c85f188af49d6bd2d`.
2. `module.cyber.kubernetes_deployment.url_analysis_browser_broker` changes only
   the image. Both brokers reported zero active sessions before rollout, and
   AWS listed no active Browser sessions.
   Plan SHA-256: `2881138cb965e2c7c1993470c17d9322539336a46cfddef9a481775a4e065d22`.

Resource-version and old-image checks guarded the ScaledJob, warm pool and
prepull image patches. The warm pool remains at zero. Both broker replicas became
healthy on the new digest. The whole-module infrastructure hold remains in force;
this release does not activate the separate protected-worker migration.

The broker still owns browser transport and protects private addresses. Explicit
host scope remains available. Credential/form submission, downloads and challenge
bypass restrictions remain. Schema, reference and artifact-integrity checks remain;
they do not select the model's verdict. Cleanup failures are reported separately.

## Hosted functional canary

The canary used the actual ScaledJob service account, labels, annotations and
archive/browser environment. It exercised the deployed image and broker:

- Athena returned 30 index candidates; query `1f24d948-5372-4b90-9f53-94799a8f5017`
  scanned 11,565,859 bytes.
- A selected 951-byte compressed WARC record was retrieved through S3, preserved,
  parsed and extracted. Original response bytes and extracted content were saved.
- A public control's observed link was followed to a different public hostname
  in the same browser session. The second observation was partial; the canary
  does not claim complete destination-page capture.
- A scripted combined assessment was retained without requiring a separate final
  review or forcing `inconclusive` because of that partial observation. This is a
  functional contract check, not evidence of model detection accuracy.
- The worker uploaded and read back 11 artifacts and verified SHA-256 hashes.
  The operator did not read protected evidence-bucket objects.
- AWS independently confirmed session `01M39Z0PAGK94QGPXXWH0M0PF9` was `TERMINATED`.
- The first canary stopped before archive retrieval because its fixture compared
  Athena's string HTTP status with an integer. Correcting that fixture produced
  the successful run; no product code or policy was changed for this correction.
- Temporary canary Jobs and ConfigMaps were removed after verification.

Acceptance metadata is in the evidence bucket at
`tenant=adp-default/issue=0/run=model-assessment-release-20260924/canary/acceptance.json`.

Implementation validation passed 354 Python tests, 20 targeted follow-up tests,
three mocked Terraform tests, Terraform validation/formatting and Ruff. All
applicable implementation PR CI checks completed successfully, including URL
browser fixtures, worker IAM/deployment safety, kernel isolation, production-image
security, webhook unit tests and package verification.

## Independent May URL comparison

The May submissions in #494, #497, #500, #503, #505, #511, #513 and #515 contain
26 submitted rows, or 25 unique URLs. The repeated URL is analyzed once in the
new run. The later CDP run on #497 is the historical comparison baseline.

Five fresh issues were triggered on 24 September, with five URLs each:
[#5904](https://github.com/aws-e/adp/issues/5904),
[#5905](https://github.com/aws-e/adp/issues/5905),
[#5906](https://github.com/aws-e/adp/issues/5906),
[#5907](https://github.com/aws-e/adp/issues/5907),
[#5908](https://github.com/aws-e/adp/issues/5908).

The prompts contain no historical verdicts, feed labels, incident narratives or
links to previous reports. The model may select independently sourced context.
This differs from May's context-rich submissions, and several May claims exceeded
their captured evidence. Historical and current verdict differences therefore
cannot by themselves establish detection accuracy. Pages and threat infrastructure
may have changed between May and September. The configured archive lookup covers
three recent crawl partitions, not an exhaustive historical search.

The preserved platform runtime automatically injects five recent persona memory
summaries and five general component records. The inspected persona summaries
reference the earlier September batches and contain no May per-URL verdicts.
The issue instructions prohibit using previous investigations, but the run is
not a fully memory-isolated blind benchmark. No conclusion should claim otherwise.

Target datasets, captures and case artifacts remain in AWS or the requested
GitHub issues; they are not included in this deployment record.
