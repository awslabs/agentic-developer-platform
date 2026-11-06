# Superplane factory delivery: one epic, medium stories, grouped releases

This is the implementation handoff for the 15 October factory architecture. ADP delivery flow owns strategic implementation. A separate recovery track makes the existing workspace usable; this backlog consumes that track's verified adoption map and does not compete for its live workspace or deployment lock.

Published epic: [#8950](https://github.com/aws-e/adp/issues/8950), with 68 native child stories: 57 implementation stories and 11 release, verification and cutover stories. Publication does not start the delivery flow. See [the eight logical workstreams and story index](workstreams.md), [parallel ownership rules](parallelism-review.md), [the dependency graph](delivery-stages.json) and [the verified issue mapping](story-mapping.json).

**End-to-end architecture:** start with the [workspace and training overview](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/README.md). It covers management-plane placement, Account Factory → Networking Factory → Cluster Factory → Workload Management, central Argo, capacity acquisition and training results. The [reconciliation design](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/control-plane-and-reconciliation.md), [database design](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/database-design.md) and [API/CLI design](https://github.com/aws-e/adp/blob/55c340f5c5b949bbdf4f9ac2c5646a868e4886d2/doc/superplane/architecture/api-design.md) define the detailed contracts. These are target-design documents; the stories below implement and verify their missing capabilities.

## Source and scope

The authoritative target design is doc/superplane/architecture/{README.md,control-plane-and-reconciliation.md,database-design.md,api-design.md} at 55c340f5c5b949bbdf4f9ac2c5646a868e4886d2. The backlog captures all four layers, AWS-first workspace delivery, Azure/GCP/Nebius adapters, capacity-aware training, optional MLflow, and migration from the current setup. Four factories are resource/controller boundaries; they do not require four independently deployed microservices.

The registered parent product/project is ADP Platform (project 470fb7f1-aa72-4544-a92d-b6cfedf1e761; product 48b53e75-610f-4483-82df-ff1739be23af), verified through the current ADP API on 26 October. Superplane remains an optional domain. The new epic is a native child of umbrella #4910. Catalog module/feature IDs are not fabricated: the initial contract owner resolves them before flow dispatch; repository paths remain source references.

## Logical grouping

The roadmap has eight workstreams: shared foundation (8), AWS accounts/networking (5), clusters/application platform (7), workspace UI/CLI (2), lifecycle/migration (6), training/capacity (15), additional providers (14), and deployment/verification (11). [The grouped index](workstreams.md) lists every story and each workstream's entry points; [workstreams.json](workstreams.json) makes the grouping machine-readable. The hierarchy remains one epic with 68 native children. These groups clarify ownership and do not replace each story's dependency rules.

Release scope is R1: 32 stories, R2: 17, R3: 19. This is the whole end-state roadmap, including multi-cloud training, rather than a reliable 2–3-day delivery estimate. The current workspace repair remains separate and immediate.

## Delivery ownership and story size

Each implementation story owns one bounded component outcome and closes only after its implementation, review and relevant tests are merged. Its release/acceptance handoff is explicit. ADP delivery flow assigns the implementation owner; the epic coordinator owns integration and acceptance rollup. Operations stories own the named release or live scenario on a selected authorized target.

All stories are medium, with three decisive acceptance cases. Common contracts and schema owners go first. C02, C03 and T01 migrations are serialized; other developers do not independently add competing heads. K02/K03 separate grant recovery from component recovery; M03/M04 separate restoration from upgrade; T07/T14 separate cancellation from checkpoint continuation; T10/T15 separate planning UI from run UI. If implementation uncovers another independent outcome, the coordinator splits it before dispatch rather than enlarging the story.

The requested 2–3-day window is a scheduling target, not acceptance evidence. The graph permits parallel work after its real dependencies. Runtime credentials, account-vending permissions, private connectivity and live provider fixtures must be selected by the delivery owner before the relevant operations assignment. Missing Azure/GCP/Nebius access blocks that provider's live proof, not unrelated implementation or AWS acceptance.

## Exactly where deployment is grouped

Implementation stories do not deploy and do not each wait for a full deployment. They run relevant source/contract/integration checks and contribute to a release manifest. CI builds needed to validate a story remain allowed; full candidate packaging is coalesced at the release checkpoint. Three planned Superplane release batches are:

| Batch | Release owner | Contents | Live verification |
| --- | --- | --- | --- |
| R1 | R01 | Shared contracts/runtime, AWS factories, Argo applications, status/UI/CLI, migration/restore support | V01 AWS lifecycle; V07 account-vending proof independently; R04 cutover using the SAME qualified artifacts |
| R2 | R02 | Training contracts, planning/admission, Trainer/JobSet/Kueue, capacity, results, UI/CLI/agent and tracking adapters | V02 bounded training/recovery/cleanup |
| R3 | R03 | Azure, GCP and Nebius adapters | V03/V04/V05 independent provider qualification; V06 cross-provider checkpoint proof |

R2 and R3 implementation can proceed in parallel once their core dependencies exist. Their shared schema/release writers are serialized by the existing deployment lock. Neither must wait for unrelated provider fixtures to run its source checks. Build each changed component once from the selected batch source and reuse unchanged digest-pinned artifacts. A genuine repair can require another rollout; do not hide that as a successful original batch.

Gateway is continuously auto-built/deployed from main. Do not freeze its pipeline, disable unrelated releases, revert it to a stale baseline or wait for an unrelated workflow's finalization as a substitute for checking serving state. C08 makes the Superplane batch verify the ACTUAL serving Gateway replicas and scheduler source, schema, identity/transport and worker binding before/after activation. Preserve concurrent unrelated Gateway changes. Keep new shared Gateway code dormant/backward compatible until the matched Superplane release is enabled.

The current superplane-api-build.yml is push-triggered; superplane-k8s-deploy.yml is dispatch-only and is not a complete native-factory rollout lane. C08 owns the exact build/installer/workflow/evidence contract before operations may run. Release stories use that verified manifest; they must not infer that existing workflow names already deploy all new components.

## Ownership and migration

Keep current Terraform as the AWS executor and authoritative resource owner initially. Factory controllers use the existing approval, budget, credential, provider inventory, lease/fence and mutation-operation services. No new retry owner or second cloud inventory is introduced. Argo owns maintained application declarations, while execution owns individual TrainJobs. Never let Terraform/ACK/another controller actively manage the same external resource.

M01 captures and shadow-binds stable workspace/account/cluster IDs, provider and credential bindings, namespace and object UIDs, Terraform backend keys/lineage/serials, original operation/approval/attempt/fence history and cleanup exposure. Private values remain in protected evidence, not issue bodies. M02 provides the one-writer cutover transaction and evaluated fallback. R04 requires a healthy repaired source workspace and fresh comparison; it changes ownership using an already qualified release, without rebuilding or recreating infrastructure. Long-term cloud-provider migration is not required to adopt the factories.

## Existing backlog and the tactical track

Old epics #5400, #6470, #6471, #6475, #6476, #6477, #6479, #6483 and #7597 retain their original acceptance obligations. #6970 retains current-workspace/demo acceptance. Reuse merged code and qualifying evidence; a closed code story is not live acceptance. New stories own the factory-contract/integration delta and cite the earlier outcome owner. Do not dispatch an old and a new issue for the same implementation simultaneously; the coordinator selects one implementation owner and links its result to both relevant acceptance owners. Do not reparent or close existing issues merely to file this roadmap.

The current workspace repair may contribute reviewed changes and evidence. Strategic implementation must rebase/reuse these, preserve the current installation and leave it running until R04 has an approved, verified cutover. The recovery track's private adoption map is an input, not a public source artifact.

## Handoff to ADP delivery flow

Use the published epic and native children as the requirements source, and delivery-stages.json as the explicit dependency/batch map. Story aliases such as C01 are document-local references, not invented provider issue IDs. story-mapping.json records real GitHub identities after publication.

The flow planner must bind the verified repository/project, completion owners, catalog aliases, existing review/merge policy and authorized target manifests. Set bounded spend, duration, retry and concurrency policy explicitly; this document does not invent a budget or accept/launch a flow. Assign implementation nodes separately from release/verification nodes and retain the three batch boundaries. Existing installation/account-creation/account-closure approval rules remain in force.

Start with C01. Its completed contracts allow C02, C08 and W03 to proceed in parallel; dispatch later inventory, AWS networking/cluster, management Argo, experience and provider/training stories as their own dependencies complete. Release R01 waits for its coherent source set; R02/R03 reuse that foundation. Keep all story/gate addresses stable in later amendments and use verified merge/release predecessor receipts.

## Author validation

validation.json records structural checks: unique aliases, acyclic dependencies, existing source pointers, medium sizing, three acceptance/scenario mappings per story, body size and secret/dispatch-marker screening. It does not claim implementation, live validation or accepted execution. The delivery owner still checks actual repository/project/target authority and each provider's first runnable fixture.
