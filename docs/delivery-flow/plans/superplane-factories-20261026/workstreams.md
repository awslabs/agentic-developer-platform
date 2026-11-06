# Logical workstreams for the 68-story roadmap

The 68 stories cover the complete proposed end state. They are grouped below into eight workstreams under one coordinating epic. Workstream labels describe ownership; they are not global scheduling barriers. Dispatch each story when its own predecessors in [delivery-stages.json](delivery-stages.json) are satisfied. Medium describes an individual story, not the size of a workstream.

Read the [end-to-end workspace and training architecture](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/README.md) first. The [handoff entry point](README.md) links its reconciliation, database and API/CLI contracts. This workstream index maps that target design to delivery tasks; it does not claim the end state is already implemented.

| Workstream | Stories | Outcome |
| --- | ---: | --- |
| G01 — Shared foundation | 8 | Contracts, durable state, reconciliation, APIs and release batching. |
| G02 — AWS accounts and networking | 5 | Existing/new accounts, network ownership, private transport and service access. |
| G03 — Clusters and application platform | 7 | EKS, recoverable bootstrap, central Argo, application profiles and readiness. |
| G04 — Workspace UI and CLI | 2 | Visible workspace progress, current status and recovery through ordinary clients. |
| G05 — Lifecycle and migration | 6 | Drift, retirement, shadow adoption, fenced cutover, restore and upgrades. |
| G06 — Training and capacity | 15 | Plan comparison, approval, jobs, GPU capacity, results, cancellation and checkpoint continuation. |
| G07 — Additional cloud providers | 14 | Azure, GCP and Nebius account/environment, networking and cluster adapters. |
| G08 — Deployment and verification | 11 | Three grouped releases, independent live acceptance and existing-workspace cutover execution. |
| **Total** | **68** | 57 implementation; 11 release, verification and cutover stories. |

## Release scope and timing

R1 contains 32 stories: 28 implementation stories plus release, AWS lifecycle verification, account-creation verification and cutover execution. R2 contains 17 stories: 15 training implementation stories plus release and verification. R3 contains 19 stories: 14 provider implementation stories plus release and four verification stories. These are three grouped Superplane releases, not 68 deployments. R04 uses qualified R1 artifacts without another build. Training and provider source work can overlap after their own core prerequisites; a release boundary is not a source-development barrier. Gateway continues its normal rolling deployments.

The full roadmap has 18 dependency edges on its longest source-and-acceptance path. Multiple parallel workers help independent branches, but cannot erase these dependencies. Treat 2–3 days as a target to assess against delivery capacity and provider access, not a completion estimate for the whole roadmap. The current workspace repair is a separate immediate track and is not waiting for this roadmap.

## G01: Shared foundation (8 stories)

Contracts, durable state, reconciliation, APIs and release batching.

Start C01; then C02, C08 and W03 can branch out. Other core stories follow their own predecessors.

| Story | Outcome | Release |
| --- | --- | --- |
| [C01 / #8951](https://github.com/aws-e/adp/issues/8951) | Version the four factory lifecycle contracts | R1 |
| [C02 / #8952](https://github.com/aws-e/adp/issues/8952) | Extend provider environments and immutable provisioning profiles | R1 |
| [C03 / #8955](https://github.com/aws-e/adp/issues/8955) | Persist factory revisions, operations and observations | R1 |
| [C04 / #8958](https://github.com/aws-e/adp/issues/8958) | Bind factory changes to existing execution and service authority | R1 |
| [C05 / #8961](https://github.com/aws-e/adp/issues/8961) | Reconcile factory steps across worker loss and uncertain responses | R1 |
| [C06 / #8959](https://github.com/aws-e/adp/issues/8959) | Expose fresh factory and workspace status through the API | R1 |
| [C07 / #8962](https://github.com/aws-e/adp/issues/8962) | Publish approved desired-state bundles with a durable outbox | R1 |
| [C08 / #8953](https://github.com/aws-e/adp/issues/8953) | Batch Superplane releases while Gateway continues rolling | R1 |

## G02: AWS accounts and networking (5 stories)

Existing/new accounts, network ownership, private transport and service access.

Start A01 after C02, C04 and C05; account creation and networking then branch independently.

| Story | Outcome | Release |
| --- | --- | --- |
| [A01 / #8966](https://github.com/aws-e/adp/issues/8966) | Onboard an existing AWS environment and verify its baseline | R1 |
| [A02 / #8971](https://github.com/aws-e/adp/issues/8971) | Create an AWS account through the factory controller | R1 |
| [N01 / #8972](https://github.com/aws-e/adp/issues/8972) | Expose AWS workspace networking as an owned factory resource | R1 |
| [N02 / #8977](https://github.com/aws-e/adp/issues/8977) | Establish private AWS management-to-workspace connectivity | R1 |
| [N03 / #8978](https://github.com/aws-e/adp/issues/8978) | Reconcile private workspace service access with VPC Lattice | R1 |

## G03: Clusters and application platform (7 stories)

EKS, recoverable bootstrap, central Argo, application profiles and readiness.

W03 starts after C01; W01 follows C08. Cluster execution follows the AWS network contract, then target registration and bootstrap converge.

| Story | Outcome | Release |
| --- | --- | --- |
| [K01 / #8976](https://github.com/aws-e/adp/issues/8976) | Reconcile EKS clusters through the existing Terraform executor | R1 |
| [K02 / #8982](https://github.com/aws-e/adp/issues/8982) | Reconcile every bootstrap grant and revocation state | R1 |
| [W01 / #8957](https://github.com/aws-e/adp/issues/8957) | Install and upgrade the central Argo management service | R1 |
| [W02 / #8987](https://github.com/aws-e/adp/issues/8987) | Register workspace targets with scoped Argo service identity | R1 |
| [W03 / #8954](https://github.com/aws-e/adp/issues/8954) | Package versioned workspace application profiles in OCI | R1 |
| [W04 / #8992](https://github.com/aws-e/adp/issues/8992) | Reconcile Application health and workspace platform readiness | R1 |
| [K03 / #8988](https://github.com/aws-e/adp/issues/8988) | Checkpoint and resume workspace component installation | R1 |

## G04: Workspace UI and CLI (2 stories)

Visible workspace progress, current status and recovery through ordinary clients.

U01 and U02 run in parallel after their API/contract predecessors.

| Story | Outcome | Release |
| --- | --- | --- |
| [U01 / #8964](https://github.com/aws-e/adp/issues/8964) | Show factory progress, dependencies and recovery in the workspace UI | R1 |
| [U02 / #8965](https://github.com/aws-e/adp/issues/8965) | Expose factory discovery and status through the installed ADP CLI | R1 |

## G05: Lifecycle and migration (6 stories)

Drift, retirement, shadow adoption, fenced cutover, restore and upgrades.

M01 can inventory after C03 and K01. Active cutover support follows platform readiness and authority; retirement has its own ownership checks.

| Story | Outcome | Release |
| --- | --- | --- |
| [L01 / #8993](https://github.com/aws-e/adp/issues/8993) | Observe cloud drift and dispatch only policy-approved corrections | R1 |
| [L02 / #8994](https://github.com/aws-e/adp/issues/8994) | Retire factory resources with explicit ownership and retention | R1 |
| [M01 / #8983](https://github.com/aws-e/adp/issues/8983) | Inventory and shadow-adopt existing Superplane workspaces | R1 |
| [M02 / #8996](https://github.com/aws-e/adp/issues/8996) | Transfer reconciliation ownership with a reversible cutover fence | R1 |
| [M03 / #8999](https://github.com/aws-e/adp/issues/8999) | Restore factory control-plane state before enabling mutation | R1 |
| [M04 / #9000](https://github.com/aws-e/adp/issues/9000) | Upgrade factory controllers without losing pending work | R1 |

## G06: Training and capacity (15 stories)

Plan comparison, approval, jobs, GPU capacity, results, cancellation and checkpoint continuation.

T01 follows the shared schema chain; T04 follows C01/W03. Planning, runtime, capacity and experience branch as their own contracts become available.

| Story | Outcome | Release |
| --- | --- | --- |
| [T01 / #8960](https://github.com/aws-e/adp/issues/8960) | Persist training requests, options and selected plan revisions | R2 |
| [T02 / #8963](https://github.com/aws-e/adp/issues/8963) | Compare capacity options without provisioning resources | R2 |
| [T03 / #8970](https://github.com/aws-e/adp/issues/8970) | Admit only the chosen current training plan | R2 |
| [T04 / #8956](https://github.com/aws-e/adp/issues/8956) | Package a compatible Trainer, JobSet and Kueue profile | R2 |
| [T05 / #8995](https://github.com/aws-e/adp/issues/8995) | Dispatch one durable TrainJob through the workspace API | R2 |
| [T06 / #8997](https://github.com/aws-e/adp/issues/8997) | Expose training progress, logs and verified output artifacts | R2 |
| [T07 / #9001](https://github.com/aws-e/adp/issues/9001) | Cancel training and verify budget-related resource cleanup | R2 |
| [T08 / #8998](https://github.com/aws-e/adp/issues/8998) | Acquire SkyPilot capacity for approved queued demand | R2 |
| [T09 / #9002](https://github.com/aws-e/adp/issues/9002) | Join allocated machines and prove GPU node readiness | R2 |
| [T10 / #8975](https://github.com/aws-e/adp/issues/8975) | Add capacity comparison and plan approval to the UI | R2 |
| [T11 / #9005](https://github.com/aws-e/adp/issues/9005) | Add training planning and execution commands to the CLI | R2 |
| [T12 / #9008](https://github.com/aws-e/adp/issues/9008) | Connect GitHub training intent to durable Superplane runs | R2 |
| [T13 / #9003](https://github.com/aws-e/adp/issues/9003) | Attach optional MLflow tracking to training results | R2 |
| [T14 / #9006](https://github.com/aws-e/adp/issues/9006) | Resume an approved run from a verified checkpoint | R2 |
| [T15 / #9009](https://github.com/aws-e/adp/issues/9009) | Show run health, cancellation and results in the UI | R2 |

## G07: Additional cloud providers (14 stories)

Azure, GCP and Nebius account/environment, networking and cluster adapters.

P01, P04 and P07 start in parallel after C02/C04/C05. Each provider then follows its own networking, infrastructure recipe and cluster path.

| Story | Outcome | Release |
| --- | --- | --- |
| [P01 / #8967](https://github.com/aws-e/adp/issues/8967) | Onboard Azure environments and verify their baseline | R3 |
| [P04 / #8968](https://github.com/aws-e/adp/issues/8968) | Onboard GCP environments and verify their baseline | R3 |
| [P07 / #8969](https://github.com/aws-e/adp/issues/8969) | Onboard Nebius environments and verify their baseline | R3 |
| [P02 / #8979](https://github.com/aws-e/adp/issues/8979) | Reconcile private Azure workspace networking | R3 |
| [P05 / #8980](https://github.com/aws-e/adp/issues/8980) | Reconcile private GCP workspace networking | R3 |
| [P08 / #8981](https://github.com/aws-e/adp/issues/8981) | Reconcile private Nebius workspace networking | R3 |
| [P03 / #8989](https://github.com/aws-e/adp/issues/8989) | Implement the Azure cluster factory adapter | R3 |
| [P06 / #8990](https://github.com/aws-e/adp/issues/8990) | Implement the GCP cluster factory adapter | R3 |
| [P09 / #8991](https://github.com/aws-e/adp/issues/8991) | Implement the Nebius cluster factory adapter | R3 |
| [P10 / #8973](https://github.com/aws-e/adp/issues/8973) | Create Azure subscriptions through approved billing authority | R3 |
| [P11 / #8974](https://github.com/aws-e/adp/issues/8974) | Create GCP projects through approved organization authority | R3 |
| [P12 / #8984](https://github.com/aws-e/adp/issues/8984) | Define the reviewed Azure cluster infrastructure recipe | R3 |
| [P13 / #8985](https://github.com/aws-e/adp/issues/8985) | Define the reviewed GCP cluster infrastructure recipe | R3 |
| [P14 / #8986](https://github.com/aws-e/adp/issues/8986) | Define the reviewed Nebius cluster infrastructure recipe | R3 |

## G08: Deployment and verification (11 stories)

Three grouped releases, independent live acceptance and existing-workspace cutover execution.

R01 assembles the AWS/core source set. R02 and R03 consume that foundation; provider verification is independent. R04 reuses qualified R1 artifacts.

| Story | Outcome | Release |
| --- | --- | --- |
| [R01 / #9004](https://github.com/aws-e/adp/issues/9004) | Deploy one compatible AWS factory release | R1 |
| [V01 / #9010](https://github.com/aws-e/adp/issues/9010) | Qualify AWS creation, interruption and retirement on the deployed release | R1 |
| [R04 / #9013](https://github.com/aws-e/adp/issues/9013) | Cut over the repaired existing workspace using the qualified release | R1 |
| [R02 / #9012](https://github.com/aws-e/adp/issues/9012) | Deploy one compatible training and capacity release | R2 |
| [V02 / #9017](https://github.com/aws-e/adp/issues/9017) | Qualify planning, training, recovery and verified cleanup | R2 |
| [R03 / #9007](https://github.com/aws-e/adp/issues/9007) | Deploy additional provider adapters as one compatible batch | R3 |
| [V03 / #9014](https://github.com/aws-e/adp/issues/9014) | Qualify the Azure workspace lifecycle | R3 |
| [V04 / #9015](https://github.com/aws-e/adp/issues/9015) | Qualify the GCP workspace lifecycle | R3 |
| [V05 / #9016](https://github.com/aws-e/adp/issues/9016) | Qualify the Nebius workspace lifecycle | R3 |
| [V06 / #9018](https://github.com/aws-e/adp/issues/9018) | Qualify an explicitly approved cross-provider checkpoint continuation | R3 |
| [V07 / #9011](https://github.com/aws-e/adp/issues/9011) | Qualify new AWS account creation and baseline recovery | R1 |

