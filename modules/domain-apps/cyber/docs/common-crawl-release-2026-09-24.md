# Common Crawl cyber release — 24 September 2026

This scoped dev maintenance release adds an Athena archive lookup and a recorded,
source-linked hypothesis before live browser investigation. Browser sessions run
in supervised processes so a stalled driver can be terminated independently.
Archive absence, archive unavailability and live capture failure remain separate
coverage outcomes. This deployment does not establish improved detection accuracy.

## Release identity and scope

- AWS account: `879318057152`; region: `us-east-1`; profile: `embark1`.
- Implementation: #5842, merged as
  `251d7d867f1284ac74b5864474032fe42703f15e`.
- Release source: `bba34f36582af8fa2a901eeb7b86b7f572272c44`. It preserves
  deployed worker source `de3227b36c7956c181cfd486876c90c19bec354e`, replacing
  only the cyber agent subtree with `61b759bd773f8ab6f26015cc0840b8a173f498d3`.
- Worker and broker image:
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:6ee9ad36ba31666872a6d653d1a3814a2f8308c99b8ac36778239dc88c7aa204`.
- Successful CodeBuild:
  `adp-dev-agent-runtime:42050bb6-096d-412f-a385-7c3cc5d8fcc1`.

The automatic image workflow could not acquire a deployment runner. The existing
operator CodeBuild path built the immutable release with `PUBLISH_LATEST=false`.

An inspected, saved Terraform plan created nine Common Crawl resources and moved
eight existing broker addresses into `module.cyber` without changing those
physical resources. A second saved plan changed only the broker image. Guarded
Kubernetes patches updated the ScaledJob, warm pool and prepull image, preserving
other workload configuration and active jobs. The warm pool remains at zero.
The whole-module webhook infrastructure hold remains in force.

## Archive configuration

| Setting | Value |
| --- | --- |
| Athena database | `adp_dev_cyber_common_crawl` |
| Table | `ccindex` |
| Workgroup | `adp-dev-cyber-common-crawl` |
| Results bucket | `adp-dev-cyber-common-crawl-879318057152` |
| Crawl partitions | `CC-MAIN-2026-39`, `CC-MAIN-2026-34`, `CC-MAIN-2026-30` |

The lookup uses the existing worker role. The future protected-worker role denies
direct Athena/S3 access; mediated archive access remains a prerequisite for that
separate cutover. No worker boundary, evidence-bucket restriction or network policy
was weakened for this release.

## Hosted acceptance

The canary ran using the actual ScaledJob labels, annotations and worker role.
Initial fixtures lacked the worker network-policy selector and could not reach
Athena. Correcting the fixture labels resolved that failure without a policy change.

- Archive hit: 30 records; query `93b2b855-da52-499a-b230-1ed8af8fb7a5`;
  11,565,859 bytes scanned.
- Archive miss: unique synthetic subdomain; query
  `0a4f6c58-ca19-4237-b4a3-e649ae5bc3e3`; 2,422,530 bytes scanned.
- `prepare → hypothesize → browse → close` captured complete DOM, screenshot
  and evidence inventory for the public control.
- The worker uploaded and read back seven artifacts, verifying SHA-256 hashes.
  The operator did not read these objects: the bucket explicitly denies operator
  object access, and that restriction was preserved.
- Direct worker access to AgentCore was denied. The operator independently
  verified browser session `01M39QSR03F6DVAZ0J9FWX8QPZ` was `TERMINATED`.
- A separate synthetic fault check ran the deployed supervisor with the actual
  broker role. Its child opened a blank AWS browser session and deliberately
  stalled. The 20-second test deadline killed the child, terminated session
  `01M39RMXQZHVKV3W2HC7NEZQCN` and released manager capacity in 20.28 seconds.
  The operator independently confirmed termination. No target page was loaded.
- Synthetic canary Jobs and ConfigMaps were removed after verification.

Canary artifacts remain in the cyber evidence bucket under
`tenant=adp-default/issue=0/run=common-crawl-release-20260924/canary/`.

Implementation validation passed 326 URL tests, nine integration checks, three
mock Terraform plans and implementation PR CI. Those checks complement the hosted
canary; they do not substitute for investigation outcomes.

## Independent GitHub rerun

All 19 user-supplied URLs were deduplicated and triggered in fresh issues:

| Issue | URL count |
| --- | ---: |
| [#5880](https://github.com/aws-e/adp/issues/5880#issuecomment-5814602673) | 5 |
| [#5881](https://github.com/aws-e/adp/issues/5881#issuecomment-5814963559) | 5 |
| [#5882](https://github.com/aws-e/adp/issues/5882#issuecomment-5814905905) | 5 |
| [#5883](https://github.com/aws-e/adp/issues/5883#issuecomment-5814625573) | 4 |

Inputs contain no earlier findings, classifications, reputation scores or feed
references. Each agent was instructed to publish its own progress and evidence
links. Target captures remain in AWS; no target dataset is included in this record.

All four runs completed and their final reports account for all 19 targets. The
operator independently verified all 16 recorded browser session IDs were
`TERMINATED`; both brokers reported zero active sessions before routing changed.
The agents published the findings themselves. A publication-only follow-up on
#5880 adds an artifact index for the first three cases whose progress comments
omitted clickable links; it does not repeat or revise the investigations.

Signed artifact links expire when the signing role credentials expire, even if
the URL requests seven days. The GitHub reports and S3 object paths are durable;
the signed access URLs are temporary.

Some real Athena queries exceeded the 45-second limit:
AWS reported roughly 42–44 seconds of service processing and zero bytes scanned.
Those cancellations are recorded as unavailable archive context, not no-match
results. Live screenshot and startup timeouts have also occurred; affected cases
retain their limitations instead of receiving an unsupported clean verdict.
Residual query and capture reliability is tracked separately as `adp-6qg`.

## Session-owner routing

After the old investigation and all four new runs finished, a freshly reviewed
saved plan changed exactly two resources: the broker Deployment and Service.
Both healthy replicas now inject `URL_ANALYSIS_SESSION_OWNER` from `status.podIP`,
use capacity-aware `/readyz`, and run with Service affinity `None`.

Plan SHA-256:
`1bff84a4d2e30e889cc3e365debacbd75e94d1ac22a0a72f8b36764160d934ea`.
The earlier plan's embedded EKS token expired while waiting for the runs to drain;
that apply was rejected without resource updates. Regenerating credentials and
verifying an identical resource diff resolved it. Generate and apply these scoped
plans close together rather than retaining an EKS token across the drain wait.

The dedicated worker canary confirmed an owner-prefixed capability, two complete
observations in the same browser session, and successful client close. Session
`01M39T3CCX4KASWJ0ZCXQ6KR7S` was independently confirmed `TERMINATED` in AWS.
Its acceptance metadata is stored at
`tenant=adp-default/issue=0/run=common-crawl-release-20260924/owner-routing-acceptance.json`
in the cyber evidence bucket. All synthetic canary Jobs and ConfigMaps were removed.
Worker, warm-pool and prepull image pins and worker archive configuration were
rechecked after the rollout.
